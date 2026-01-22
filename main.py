import os
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: cria tabelas
    if engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Limpa áudios antigos (mais de 1 hora)
        async with async_session() as session:
            cutoff = datetime.utcnow() - timedelta(hours=1)
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

        # Salva no banco e envia botões de confirmação
        await save_pending_audio(chat_id, message_id, base_url, token)
        await send_confirmation_buttons(chat_id, message_id, base_url, token)

        return {"status": "ok", "action": "awaiting_confirmation"}

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
        audio_message_id = button_id[4:]  # Remove "sim_"
        await process_transcription(chat_id, audio_message_id, base_url, token)
    elif button_id.startswith("nao_"):
        audio_message_id = button_id[4:]  # Remove "nao_"
        await remove_pending_audio(audio_message_id)
        from_number = chat_id.replace("@s.whatsapp.net", "")
        await send_message(from_number, "Ok! O áudio não será transcrito.", base_url, token)

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


async def remove_pending_audio(message_id: str):
    """
    Remove áudio pendente do banco de dados
    """
    if not async_session:
        return

    async with async_session() as session:
        await session.execute(delete(PendingAudio).where(PendingAudio.message_id == message_id))
        await session.commit()


async def send_confirmation_buttons(chat_id: str, message_id: str, base_url: str, token: str):
    """
    Envia botões pedindo confirmação para transcrever
    """
    try:
        from_number = chat_id.replace("@s.whatsapp.net", "")

        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{base_url}/send/menu"

            headers = {
                "Content-Type": "application/json",
                "token": token
            }

            payload = {
                "number": from_number,
                "type": "button",
                "text": "Recebi seu áudio! Deseja que eu faça a transcrição?",
                "choices": [
                    f"Sim, transcrever|sim_{message_id}",
                    f"Não, obrigado|nao_{message_id}"
                ],
                "footerText": "A transcrição será feita por IA"
            }

            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                print(f"Botões de confirmação enviados para {from_number}")
            else:
                print(f"Erro ao enviar botões: {response.status_code} - {response.text}")

    except Exception as e:
        print(f"Erro ao enviar botões: {e}")


async def process_transcription(chat_id: str, message_id: str, base_url: str, token: str):
    """
    Processa a transcrição após confirmação do usuário
    """
    from_number = chat_id.replace("@s.whatsapp.net", "")

    # Busca dados do banco (caso base_url/token venham vazios)
    pending = await get_pending_audio(message_id)
    if pending:
        base_url = pending.base_url or base_url
        token = pending.token or token

    # Baixa o áudio
    audio_bytes = await download_audio_via_uazapi(base_url, token, message_id)

    if not audio_bytes:
        await send_message(from_number, "Não consegui baixar o áudio. Tente enviar novamente.", base_url, token)
        await remove_pending_audio(message_id)
        return

    # Transcreve
    transcription = await transcribe_audio(audio_bytes)

    if transcription:
        await send_message(from_number, f"*Transcrição:*\n\n{transcription}", base_url, token)
    else:
        await send_message(from_number, "Não consegui transcrever o áudio. Tente novamente.", base_url, token)

    # Remove do banco
    await remove_pending_audio(message_id)


async def download_audio_via_uazapi(base_url: str, token: str, message_id: str) -> bytes | None:
    """
    Baixa o áudio usando o endpoint /message/download da UAZAPI
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
                return None

            result = response.json()
            file_url = result.get("fileURL")

            if not file_url:
                print(f"URL do arquivo não encontrada na resposta: {result}")
                return None

            print(f"URL do áudio obtida: {file_url}")

            audio_response = await client.get(file_url, follow_redirects=True)

            if audio_response.status_code == 200:
                print(f"Áudio baixado: {len(audio_response.content)} bytes")
                return audio_response.content
            else:
                print(f"Erro ao baixar áudio: {audio_response.status_code}")
                return None

    except Exception as e:
        print(f"Erro ao baixar áudio: {e}")
        return None


async def transcribe_audio(audio_bytes: bytes) -> str | None:
    """
    Transcreve o áudio usando Google Gemini
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
            return response.text.strip() if response.text else None

        finally:
            os.unlink(temp_path)

    except Exception as e:
        print(f"Erro na transcrição: {e}")
        return None


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
