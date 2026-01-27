import os
import random
import asyncio
import tempfile
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import httpx
import google.generativeai as genai
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, DateTime, select, delete

load_dotenv()

# Configurações
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Mensagens de erro amigáveis (com variações engraçadas)
ERROR_MESSAGES = {
    "download_failed": [
        "Ops! O áudio fugiu antes de eu conseguir pegar. Envia de novo?",
        "Eita! O arquivo deu uma de Houdini e sumiu. Manda novamente!",
        "Ih, o áudio tava tímido e se escondeu. Tenta enviar de novo!",
        "Falha no download! Parece que o arquivo tirou férias. Envia outro!",
        "O áudio não quis colaborar hoje. Bora tentar de novo?",
    ],
    "transcription_failed": [
        "Ops! Meus ouvidos digitais não entenderam nada. Tenta um áudio mais claro?",
        "Desculpa, mas esse áudio me deixou confuso. Será que tá muito baixo ou com ruído?",
        "Hmm, não consegui decifrar esse áudio. Parece código secreto! Envia outro?",
        "Minha IA teve um branco nesse áudio. Tenta mandar de novo!",
        "Esse áudio me pegou de surpresa e não entendi nada. Bora tentar novamente?",
    ],
    "timeout": [
        "Xi, demorou demais! Até eu cochilei esperando. Tenta de novo?",
        "O servidor foi fazer um café e esqueceu de voltar. Tenta novamente!",
        "Timeout! Parece que a internet tá de preguiça hoje. Manda de novo?",
        "Eita, travou! Até a paciência de Jó não aguenta. Tenta mais uma vez!",
        "Demorou tanto que achei que era pegadinha. Envia novamente!",
    ],
    "audio_expired": [
        "Esse áudio já era! Passou das 24h e virou abóbora. Manda um novo!",
        "Áudio expirado! Ele não aguentou esperar e foi embora. Envia outro!",
        "Tarde demais! Esse áudio já aposentou. Manda um novinho!",
        "24h se passaram e o áudio disse 'tchau'. Envia de novo!",
        "Esse áudio já tem barba branca. Manda um mais jovem!",
    ],
    "service_unavailable": [
        "Sistema em manutenção! Estamos dando um tapa no visual. Volta já já!",
        "Ops! O serviço foi tomar um ar. Tenta daqui a pouquinho!",
        "Estamos com problemas técnicos. Até robô precisa de um descanso!",
        "Serviço indisponível! Parece que alguém tropeçou no cabo. Já volta!",
    ],
    "cancelled": [
        "Tudo bem! Se mudar de ideia, é só enviar outro áudio.",
        "Ok! Respeito sua decisão. Qualquer coisa, estou aqui!",
        "Entendido! Quando quiser transcrever, é só mandar um áudio.",
        "Sem problemas! Fico à disposição se precisar.",
    ],
    "processing": [
        "Transcrevendo seu áudio, aguarde...",
        "Colocando meus fones de ouvido digitais...",
        "Analisando cada palavrinha do seu áudio...",
        "Um momento! Estou ouvindo com atenção...",
        "Processando... prometo não demorar!",
    ],
}


def get_error_message(error_type: str) -> str:
    """Retorna uma mensagem aleatória do tipo especificado"""
    messages = ERROR_MESSAGES.get(error_type, ["Ops! Algo deu errado."])
    return random.choice(messages)

# Ajusta URL para asyncpg se vier do Railway (postgres:// -> postgresql+asyncpg://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Configura Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Database setup
engine = create_async_engine(DATABASE_URL, echo=False) if DATABASE_URL else None
async_session = async_sessionmaker(engine, expire_on_commit=False) if engine else None


class Base(DeclarativeBase):
    pass


class PendingAudio(Base):
    """Armazena áudios pendentes de confirmação"""
    __tablename__ = "pending_audios"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(50), index=True)
    message_id: Mapped[str] = mapped_column(String(100), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    token: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserTerms(Base):
    """Armazena usuários que já viram os termos de uso"""
    __tablename__ = "user_terms"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: cria tabelas
    if engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Limpa áudios antigos (mais de 24 horas)
        async with async_session() as session:
            cutoff = datetime.utcnow() - timedelta(hours=24)
            await session.execute(delete(PendingAudio).where(PendingAudio.created_at < cutoff))
            await session.commit()
    yield
    # Shutdown
    if engine:
        await engine.dispose()


app = FastAPI(title="WhatsApp Audio Transcriber", lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "ok", "message": "WhatsApp Audio Transcriber está rodando!"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/webhook")
async def webhook(request: Request):
    """
    Webhook que recebe mensagens do UAZAPI
    """
    try:
        data = await request.json()
        print(f"Webhook recebido: {data}")

        message = data.get("message", {})
        message_type = message.get("messageType", "")
        base_url = data.get("BaseUrl", "")
        token = data.get("token", "")

        # Verifica se é resposta de botão
        button_id = message.get("buttonOrListid", "")
        if button_id:
            return await handle_button_response(message, base_url, token)

        # Verifica se é uma mensagem de áudio
        if message_type != "AudioMessage":
            print(f"Mensagem ignorada (tipo: {message_type})")
            return {"status": "ignored", "reason": "not_audio"}

        # Extrai informações do webhook
        chat_id = message.get("chatid", "")
        message_id = message.get("messageid", "")

        if not message_id or not base_url or not token:
            print("Dados incompletos no webhook")
            return {"status": "error", "reason": "missing_data"}

        print(f"Áudio recebido de {chat_id}, mensagem: {message_id}")

        # Verifica se já aceitou os termos
        if await has_seen_terms(chat_id):
            # Já aceitou os termos: salva na fila e processa após delay
            already_has_pending = await has_pending_audio(chat_id)
            await save_pending_audio(chat_id, message_id, base_url, token)

            if not already_has_pending:
                # Primeiro áudio da fila: inicia o processo de coleta
                asyncio.create_task(process_queue_after_delay(chat_id, base_url, token))

            return {"status": "ok", "action": "queued_for_processing"}

        # Ainda não aceitou - salva o áudio pendente
        await save_pending_audio(chat_id, message_id, base_url, token)

        # Verifica se já tem outros áudios pendentes (já mostrou os termos)
        pending_count = len(await get_all_pending_audios(chat_id))

        if pending_count == 1:
            # Primeiro áudio desse usuário: envia os termos
            await send_confirmation_buttons(chat_id, message_id, base_url, token)
            return {"status": "ok", "action": "awaiting_confirmation"}
        else:
            # Já tem áudios pendentes, não precisa enviar termos de novo
            print(f"Áudio adicionado à fila ({pending_count} pendentes)")
            return {"status": "ok", "action": "queued"}

    except Exception as e:
        print(f"Erro no webhook: {e}")
        return {"status": "error", "message": str(e)}


async def handle_button_response(message: dict, base_url: str, token: str):
    """
    Processa a resposta do botão de confirmação
    """
    button_id = message.get("buttonOrListid", "")
    chat_id = message.get("chatid", "")

    print(f"Resposta de botão recebida: {button_id} de {chat_id}")

    # Extrai o message_id do button_id (formato: "sim_MESSAGEID" ou "nao_MESSAGEID")
    if button_id.startswith("sim_"):
        # Marca que aceitou os termos (próximos áudios serão transcritos direto)
        await mark_terms_seen(chat_id)

        # Busca TODOS os áudios pendentes e transcreve
        pending_audios = await get_all_pending_audios(chat_id)
        num_audios = len(pending_audios)
        print(f"Transcrevendo {num_audios} áudio(s) pendente(s)")

        if num_audios > 0:
            from_number = chat_id.replace("@s.whatsapp.net", "")
            # Envia mensagem de processamento apenas uma vez
            if num_audios == 1:
                await send_message(from_number, get_error_message("processing"), base_url, token)
            else:
                await send_message(from_number, f"Transcrevendo {num_audios} áudios, aguarde...", base_url, token)

            # Transcreve todos sem enviar "processando" novamente
            for pending in pending_audios:
                await process_transcription(chat_id, pending.message_id, pending.base_url, pending.token, show_processing=False)

    elif button_id.startswith("nao_"):
        # Remove TODOS os áudios pendentes
        await remove_all_pending_audios(chat_id)
        from_number = chat_id.replace("@s.whatsapp.net", "")
        await send_message(from_number, get_error_message("cancelled"), base_url, token)
        # Não marca os termos como aceitos, então vai perguntar de novo no próximo áudio

    return {"status": "ok", "action": "button_handled"}


async def save_pending_audio(chat_id: str, message_id: str, base_url: str, token: str):
    """
    Salva áudio pendente no banco de dados
    """
    if not async_session:
        print("Banco de dados não configurado")
        return

    async with async_session() as session:
        # Remove se já existir
        await session.execute(delete(PendingAudio).where(PendingAudio.message_id == message_id))

        pending = PendingAudio(
            chat_id=chat_id,
            message_id=message_id,
            base_url=base_url,
            token=token
        )
        session.add(pending)
        await session.commit()
        print(f"Áudio pendente salvo: {message_id}")


async def get_pending_audio(message_id: str) -> PendingAudio | None:
    """
    Busca áudio pendente no banco de dados
    """
    if not async_session:
        return None

    async with async_session() as session:
        result = await session.execute(
            select(PendingAudio).where(PendingAudio.message_id == message_id)
        )
        return result.scalar_one_or_none()


async def get_all_pending_audios(chat_id: str) -> list[PendingAudio]:
    """
    Busca todos os áudios pendentes de um chat
    """
    if not async_session:
        return []

    async with async_session() as session:
        result = await session.execute(
            select(PendingAudio).where(PendingAudio.chat_id == chat_id)
        )
        return list(result.scalars().all())


async def has_pending_audio(chat_id: str) -> bool:
    """
    Verifica se o chat já tem algum áudio pendente
    """
    if not async_session:
        return False

    async with async_session() as session:
        result = await session.execute(
            select(PendingAudio).where(PendingAudio.chat_id == chat_id)
        )
        return result.scalar_one_or_none() is not None


async def remove_pending_audio(message_id: str):
    """
    Remove áudio pendente do banco de dados
    """
    if not async_session:
        return

    async with async_session() as session:
        await session.execute(delete(PendingAudio).where(PendingAudio.message_id == message_id))
        await session.commit()


async def remove_all_pending_audios(chat_id: str):
    """
    Remove todos os áudios pendentes de um chat
    """
    if not async_session:
        return

    async with async_session() as session:
        await session.execute(delete(PendingAudio).where(PendingAudio.chat_id == chat_id))
        await session.commit()


async def has_seen_terms(chat_id: str) -> bool:
    """
    Verifica se o usuário já viu os termos de uso
    """
    if not async_session:
        return False

    async with async_session() as session:
        result = await session.execute(
            select(UserTerms).where(UserTerms.chat_id == chat_id)
        )
        return result.scalar_one_or_none() is not None


async def mark_terms_seen(chat_id: str):
    """
    Marca que o usuário já viu os termos de uso
    """
    if not async_session:
        return

    async with async_session() as session:
        # Verifica se já existe
        existing = await session.execute(
            select(UserTerms).where(UserTerms.chat_id == chat_id)
        )
        if existing.scalar_one_or_none() is None:
            user_terms = UserTerms(chat_id=chat_id)
            session.add(user_terms)
            await session.commit()
            print(f"Termos marcados como vistos para: {chat_id}")


async def process_queue_after_delay(chat_id: str, base_url: str, token: str, delay_seconds: int = 15):
    """
    Aguarda um tempo para coletar mais áudios e depois processa toda a fila
    """
    print(f"Aguardando {delay_seconds}s para coletar mais áudios de {chat_id}...")
    await asyncio.sleep(delay_seconds)

    # Busca todos os áudios pendentes
    pending_audios = await get_all_pending_audios(chat_id)
    num_audios = len(pending_audios)

    if num_audios == 0:
        print(f"Nenhum áudio pendente para {chat_id}")
        return

    print(f"Processando {num_audios} áudio(s) de {chat_id}")

    from_number = chat_id.replace("@s.whatsapp.net", "")

    # Envia mensagem de processamento apenas uma vez
    if num_audios == 1:
        await send_message(from_number, get_error_message("processing"), base_url, token)
    else:
        await send_message(from_number, f"Transcrevendo {num_audios} áudios, aguarde...", base_url, token)

    # Transcreve todos
    for pending in pending_audios:
        await process_transcription(chat_id, pending.message_id, pending.base_url, pending.token, show_processing=False)


async def send_confirmation_buttons(chat_id: str, message_id: str, base_url: str, token: str):
    """
    Envia botões pedindo confirmação para transcrever (apenas no primeiro contato)
    """
    try:
        from_number = chat_id.replace("@s.whatsapp.net", "")

        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{base_url}/send/menu"

            headers = {
                "Content-Type": "application/json",
                "token": token
            }

            terms_text = """Olá! Sou o *Papagaio Transcritor*, um bot que transcreve seus áudios usando Inteligência Artificial.

*Como funciona:*
• Envie um áudio e eu transformo em texto
• A transcrição é feita por IA (pode conter pequenos erros)

*Termos de Uso:*
• Seus áudios são processados apenas para transcrição
• Todas as mensagens são *apagadas após 24 horas*
• Não armazenamos o conteúdo das transcrições
• Ao clicar em "Aceitar", você concorda com estes termos

Deseja continuar?"""

            payload = {
                "number": from_number,
                "type": "button",
                "text": terms_text,
                "choices": [
                    f"Aceitar e transcrever|sim_{message_id}",
                    f"Não aceito|nao_{message_id}"
                ],
                "footerText": "Transcrição por IA"
            }

            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                print(f"Termos enviados para {from_number}")
            else:
                print(f"Erro ao enviar botões: {response.status_code} - {response.text}")

    except Exception as e:
        print(f"Erro ao enviar botões: {e}")


async def process_transcription(chat_id: str, message_id: str, base_url: str, token: str, show_processing: bool = True):
    """
    Processa a transcrição após confirmação do usuário
    show_processing: se True, envia mensagem "Transcrevendo..." antes de processar
    """
    from_number = chat_id.replace("@s.whatsapp.net", "")

    # Busca dados do banco (caso base_url/token venham vazios)
    pending = await get_pending_audio(message_id)
    if pending:
        base_url = pending.base_url or base_url
        token = pending.token or token
    elif not base_url or not token:
        # Áudio não encontrado no banco e sem credenciais - provavelmente expirou
        await send_message(from_number, get_error_message("audio_expired"), base_url, token)
        return

    # Avisa que está processando (só se não tiver sido avisado antes)
    if show_processing:
        await send_message(from_number, get_error_message("processing"), base_url, token)

    # Baixa o áudio
    audio_bytes, download_error = await download_audio_via_uazapi(base_url, token, message_id)

    if not audio_bytes:
        error_type = "timeout" if download_error == "timeout" else "download_failed"
        await send_message(from_number, get_error_message(error_type), base_url, token)
        await remove_pending_audio(message_id)
        return

    # Transcreve
    transcription, transcribe_error = await transcribe_audio(audio_bytes)

    if transcription:
        await send_message(from_number, f"*Transcrição:*\n\n{transcription}", base_url, token)
    else:
        error_type = "timeout" if transcribe_error == "timeout" else "transcription_failed"
        await send_message(from_number, get_error_message(error_type), base_url, token)

    # Remove do banco
    await remove_pending_audio(message_id)


async def download_audio_via_uazapi(base_url: str, token: str, message_id: str) -> tuple[bytes | None, str | None]:
    """
    Baixa o áudio usando o endpoint /message/download da UAZAPI
    Retorna (bytes, error_type) onde error_type pode ser "timeout", "failed" ou None
    """
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            download_url = f"{base_url}/message/download"

            headers = {
                "Content-Type": "application/json",
                "token": token
            }

            payload = {
                "id": message_id,
                "generate_mp3": True,
                "return_link": True
            }

            print(f"Baixando áudio via UAZAPI: {download_url}")
            response = await client.post(download_url, json=payload, headers=headers)

            if response.status_code != 200:
                print(f"Erro ao obter URL do áudio: {response.status_code} - {response.text}")
                return None, "failed"

            result = response.json()
            file_url = result.get("fileURL")

            if not file_url:
                print(f"URL do arquivo não encontrada na resposta: {result}")
                return None, "failed"

            print(f"URL do áudio obtida: {file_url}")

            audio_response = await client.get(file_url, follow_redirects=True)

            if audio_response.status_code == 200:
                print(f"Áudio baixado: {len(audio_response.content)} bytes")
                return audio_response.content, None
            else:
                print(f"Erro ao baixar áudio: {audio_response.status_code}")
                return None, "failed"

    except httpx.TimeoutException:
        print("Timeout ao baixar áudio")
        return None, "timeout"
    except Exception as e:
        print(f"Erro ao baixar áudio: {e}")
        return None, "failed"


async def transcribe_audio(audio_bytes: bytes) -> tuple[str | None, str | None]:
    """
    Transcreve o áudio usando Google Gemini
    Retorna (transcription, error_type) onde error_type pode ser "timeout", "failed" ou None
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
            temp_file.write(audio_bytes)
            temp_path = temp_file.name

        try:
            audio_file = genai.upload_file(temp_path, mime_type="audio/mpeg")
            model = genai.GenerativeModel("gemini-2.0-flash")

            response = model.generate_content([
                audio_file,
                "Transcreva este áudio em português brasileiro. Retorne apenas a transcrição, sem comentários adicionais."
            ])

            audio_file.delete()

            if response.text:
                return response.text.strip(), None
            else:
                return None, "failed"

        finally:
            os.unlink(temp_path)

    except TimeoutError:
        print("Timeout na transcrição")
        return None, "timeout"
    except Exception as e:
        print(f"Erro na transcrição: {e}")
        return None, "failed"


async def send_message(to: str, text: str, base_url: str, token: str):
    """
    Envia mensagem de texto pelo UAZAPI
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{base_url}/send/text"

            headers = {
                "Content-Type": "application/json",
                "token": token
            }

            payload = {
                "number": to,
                "text": text
            }

            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                print(f"Mensagem enviada para {to}")
            else:
                print(f"Erro ao enviar mensagem: {response.status_code} - {response.text}")

    except Exception as e:
        print(f"Erro ao enviar mensagem: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
