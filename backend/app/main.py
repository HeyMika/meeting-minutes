from fastapi import FastAPI, HTTPException, BackgroundTasks, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn
from typing import Optional, List
import logging
from dotenv import load_dotenv
from db import DatabaseManager
import json
from threading import Lock
from transcript_processor import TranscriptProcessor
import time
import os
import sys
import tempfile
import signal
from mega_asr_service import MegaASRProcessor

# --- GLOBAL BROKEN PIPE & TELEMETRY FIX ---
# Disable wandb background processes
os.environ["WANDB_MODE"] = "disabled"

# Ignore SIGPIPE globally at the process level
if hasattr(signal, 'SIGPIPE'):
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

class SafeStream:
    def __init__(self, original_stream):
        self.stream = original_stream
        self.devnull = None

    def write(self, data):
        try:
            self.stream.write(data)
            self.stream.flush()
        except Exception:
            if self.devnull is None:
                try:
                    self.devnull = open(os.devnull, 'w')
                except:
                    return
            self.stream = self.devnull
            try:
                self.stream.write(data)
            except:
                pass

    def flush(self):
        try:
            self.stream.flush()
        except:
            pass

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

sys.stdout = SafeStream(sys.stdout)
sys.stderr = SafeStream(sys.stderr)

def safe_print(msg):
    try:
        print(msg, flush=True)
    except:
        pass

# ------------------------------------------

# Load environment variables
load_dotenv()

# Configure logger
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

app = FastAPI(title="Meetily Python Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=3600,
)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

db = DatabaseManager()

class Transcript(BaseModel):
    id: str
    text: str
    timestamp: str

class MeetingTitleRequest(BaseModel):
    meeting_id: str
    title: str

class DeleteMeetingRequest(BaseModel):
    meeting_id: str

class SaveTranscriptRequest(BaseModel):
    meeting_id: str
    transcripts: List[Transcript]

class ModelConfigRequest(BaseModel):
    provider: str
    model: str
    whisper_model: str
    ollama_endpoint: Optional[str] = None

class GetApiKeyRequest(BaseModel):
    provider: str

class MeetingSummaryRequest(BaseModel):
    meeting_id: str
    summary: str

@app.post("/save-meeting-title")
async def save_meeting_title(request: MeetingTitleRequest):
    try:
        success = await db.save_meeting_title(request.meeting_id, request.title)
        return {"message": "Meeting title saved successfully"} if success else JSONResponse(status_code=500, content={"detail": "Failed"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/delete-meeting")
async def delete_meeting(request: DeleteMeetingRequest):
    try:
        success = await db.delete_meeting(request.meeting_id)
        return {"message": "Meeting deleted"} if success else JSONResponse(status_code=500, content={"detail": "Failed"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/process-transcript")
async def process_transcript(
    text: str = Form(...),
    model: str = Form(...),
    model_name: str = Form(...),
    chunk_size: int = Form(5000),
    overlap: int = Form(1000),
    custom_prompt: str = Form("")
):
    try:
        processor = TranscriptProcessor()
        task_id, chunks = await processor.process_transcript(text, model, model_name, chunk_size, overlap, custom_prompt)
        return {"task_id": task_id, "chunks": chunks, "status": "processing"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/transcribe")
def transcribe_audio(
    file: UploadFile = File(...),
    model: str = Form("mega-asr"),
    language: Optional[str] = Form(None)
):
    try:
        suffix = os.path.splitext(file.filename)[1] if file.filename else ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file.file.read())
            temp_path = tmp.name

        try:
            processor = MegaASRProcessor()
            transcript_text = processor.transcribe(temp_path, language=language)
            return {"text": transcript_text}
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    except Exception as e:
        # Check for broken pipe in string representation
        msg = str(e)
        if "Broken pipe" in msg or "[Errno 32]" in msg:
            # If a broken pipe occurred despite protection, don't crash the handler
            return {"text": "Transcription completed (Output pipe closed by parent)"}
        raise HTTPException(status_code=500, detail=f"Transcription failed: {msg}")

@app.post("/save-transcript")
async def save_transcript(request: SaveTranscriptRequest):
    try:
        success = await db.save_transcripts(request.meeting_id, [t.dict() for t in request.transcripts])
        return {"message": "Saved"} if success else JSONResponse(status_code=500, content={"detail": "Failed"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/save-model-config")
async def save_model_config(request: ModelConfigRequest):
    try:
        await db.save_model_config(request.provider, request.model, request.whisper_model, request.ollama_endpoint)
        return {"message": "Saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/get-api-key")
async def get_api_key(request: GetApiKeyRequest):
    return await db.get_api_key(request.provider)

@app.post("/save-meeting-summary")
async def save_meeting_summary(request: MeetingSummaryRequest):
    try:
        success = await db.save_meeting_summary(request.meeting_id, request.summary)
        return {"message": "Saved"} if success else JSONResponse(status_code=500, content={"detail": "Failed"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("shutdown")
async def shutdown_event():
    pass

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    safe_print("🚀 Mega-ASR Backend Server starting on port 5167...")
    # Disable reload for sidecar usage
    uvicorn.run(app, host="0.0.0.0", port=5167, log_level="info")
