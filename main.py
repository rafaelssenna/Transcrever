import os
import tempfile
import httpx
import google.generativeai as genai
from fastapi import FastAPI, Request
from dotenv import load_dotenv

load_dotenv()

# Configurações
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configura Gemini
genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI(title="WhatsApp Audio Transcriber")


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

        # Verifica se é uma mensagem de áudio
        message = data.get("message", {})
        message_type = message.get("messageType", "")

        if message_type != "AudioMessage":
            print(f"Mensagem ignorada (tipo: {message_type})")
            return {"status": "ignored", "reason": "not_audio"}

        # Extrai informações do webhook
        from_number = message.get("chatid", "").replace("@s.whatsapp.net", "")
        message_id = message.get("messageid", "")
        base_url = data.get("BaseUrl", "")
        token = data.get("token", "")

        if not message_id:
            print("ID da mensagem não encontrado")
            return {"status": "error", "reason": "no_message_id"}

        if not base_url or not token:
            print("BaseUrl ou token não encontrados no webhook")
            return {"status": "error", "reason": "missing_credentials"}

        print(f"Processando áudio de {from_number}, mensagem: {message_id}")

        # Baixa o áudio via UAZAPI (descriptografado)
        audio_bytes = await download_audio_via_uazapi(base_url, token, message_id)

        if not audio_bytes:
            await send_message(from_number, "Não consegui baixar o áudio. Tente novamente.", base_url, token)
            return {"status": "error", "reason": "download_failed"}

        # Transcreve com Gemini
        transcription = await transcribe_audio(audio_bytes)

        if transcription:
            await send_message(from_number, f"*Transcrição:*\n\n{transcription}", base_url, token)
        else:
            await send_message(from_number, "Não consegui transcrever o áudio. Tente novamente.", base_url, token)

        return {"status": "ok", "transcription": transcription}

    except Exception as e:
        print(f"Erro no webhook: {e}")
        return {"status": "error", "message": str(e)}


async def download_audio_via_uazapi(base_url: str, token: str, message_id: str) -> bytes | None:
    """
    Baixa o áudio usando o endpoint /message/download da UAZAPI
    """
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            # Primeiro, usa o endpoint de download para obter URL pública
            download_url = f"{base_url}/message/download"

            headers = {
                "Content-Type": "application/json",
                "token": token
            }

            payload = {
                "id": message_id,
                "generate_mp3": True,  # Converte para MP3 (mais compatível)
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

            # Agora baixa o arquivo da URL pública
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
        # Salva temporariamente o áudio
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
            temp_file.write(audio_bytes)
            temp_path = temp_file.name

        try:
            # Faz upload do arquivo para o Gemini
            audio_file = genai.upload_file(temp_path, mime_type="audio/mpeg")

            # Usa Gemini para transcrever
            model = genai.GenerativeModel("gemini-2.0-flash")

            response = model.generate_content([
                audio_file,
                "Transcreva este áudio em português brasileiro. Retorne apenas a transcrição, sem comentários adicionais."
            ])

            # Remove o arquivo do Gemini após uso
            audio_file.delete()

            return response.text.strip() if response.text else None

        finally:
            # Remove arquivo temporário
            os.unlink(temp_path)

    except Exception as e:
        print(f"Erro na transcrição: {e}")
        return None


async def send_message(to: str, text: str, base_url: str, token: str):
    """
    Envia mensagem de volta pelo UAZAPI
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
