import os
import tempfile
import httpx
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

load_dotenv()

# Configurações
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN")
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
        message_type = message.get("type")

        if message_type != "audio":
            print(f"Mensagem ignorada (tipo: {message_type})")
            return {"status": "ignored", "reason": "not_audio"}

        # Extrai informações
        from_number = data.get("from", "")
        audio_data = message.get("audio", {})
        media_url = audio_data.get("url") or audio_data.get("mediaUrl")

        if not media_url:
            print("URL do áudio não encontrada")
            return {"status": "error", "reason": "no_media_url"}

        print(f"Processando áudio de {from_number}: {media_url}")

        # Baixa o áudio
        audio_bytes = await download_audio(media_url)

        if not audio_bytes:
            await send_message(from_number, "❌ Não consegui baixar o áudio. Tente novamente.")
            return {"status": "error", "reason": "download_failed"}

        # Transcreve com Gemini
        transcription = await transcribe_audio(audio_bytes)

        if transcription:
            await send_message(from_number, f"📝 *Transcrição:*\n\n{transcription}")
        else:
            await send_message(from_number, "❌ Não consegui transcrever o áudio. Tente novamente.")

        return {"status": "ok", "transcription": transcription}

    except Exception as e:
        print(f"Erro no webhook: {e}")
        return {"status": "error", "message": str(e)}


async def download_audio(url: str) -> bytes | None:
    """
    Baixa o arquivo de áudio da URL
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Adiciona token se necessário
            headers = {}
            if UAZAPI_TOKEN:
                headers["Authorization"] = f"Bearer {UAZAPI_TOKEN}"

            response = await client.get(url, headers=headers, follow_redirects=True)

            if response.status_code == 200:
                return response.content
            else:
                print(f"Erro ao baixar áudio: {response.status_code}")
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
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_file:
            temp_file.write(audio_bytes)
            temp_path = temp_file.name

        try:
            # Faz upload do arquivo para o Gemini
            audio_file = genai.upload_file(temp_path, mime_type="audio/ogg")

            # Usa Gemini para transcrever
            model = genai.GenerativeModel("gemini-1.5-flash")

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


async def send_message(to: str, text: str):
    """
    Envia mensagem de volta pelo UAZAPI
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{UAZAPI_BASE_URL}/message/text"

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {UAZAPI_TOKEN}"
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
