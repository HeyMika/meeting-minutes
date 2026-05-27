import os
import sys
from pathlib import Path
import torch
import logging
import re
import json
from threading import Lock
import signal

# --- GLOBAL BROKEN PIPE & TELEMETRY FIX ---
# Disable wandb background processes which often cause pipe issues in sidecars
os.environ["WANDB_MODE"] = "disabled"

# Ignore SIGPIPE globally at the process level (Unix/macOS only)
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
            # Absolute silence on any write error to prevent crash loops
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

# Patch streams early
sys.stdout = SafeStream(sys.stdout)
sys.stderr = SafeStream(sys.stderr)

def safe_print(msg):
    """Safe print that uses patched sys.stdout"""
    try:
        print(msg, flush=True)
    except:
        pass

# ------------------------------------------

logger = logging.getLogger(__name__)

# Add src to path for Mega-ASR imports
REPO_DIR = Path(__file__).resolve().parent / "mega_asr_repo"
sys.path.append(str(REPO_DIR / "src"))

class MegaASRProcessor:
    _instance = None
    _lock = Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(MegaASRProcessor, cls).__new__(cls)
                cls._instance.model = None
                cls._instance.initialized = False
            return cls._instance

    def initialize(self):
        with self._lock:
            if self.initialized:
                return
                
            try:
                safe_print("🚀 Loading Mega-ASR foundation model (1.7B)...")
                from MegaASR.model.megaASR import MegaASR
                
                ckpt_dir = REPO_DIR / "ckpt/Mega-ASR"
                
                # Auto-detect device
                device = "cpu"
                if torch.cuda.is_available():
                    device = "cuda"
                    safe_print("✅ Using CUDA GPU acceleration")
                elif torch.backends.mps.is_available():
                    device = "mps"
                    safe_print("✅ Using Apple Metal (MPS) acceleration")
                else:
                    safe_print("⚠️ Using CPU (this will be slow)")
                
                safe_print(f"Loading weights from {ckpt_dir}...")
                
                self.model = MegaASR(
                    model_path=ckpt_dir / "Qwen3-ASR-1.7B",
                    lora_dir=ckpt_dir / "mega-asr-merged",
                    router_checkpoint=ckpt_dir / "audio_quality_router/best_acc_model.safetensors",
                    routing_enabled=True,
                    quality_threshold=0.5,
                    device_map=device,
                    keep_delta_on_gpu=True,
                )
                self.initialized = True
                safe_print("✨ Mega-ASR model loaded and ready!")
            except Exception as e:
                safe_print(f"❌ Failed to initialize Mega-ASR: {str(e)}")
                raise

    def _deduplicate_phrases(self, text: str) -> str:
        if not text: return ""
        text = text.strip()
        
        # Handle list artifacts
        try:
            if text.startswith("[") and text.endswith("]"):
                import ast
                parsed = ast.literal_eval(text)
                if isinstance(parsed, list):
                    text = " ".join([str(i) for i in parsed])
        except:
            text = re.sub(r"^\[['\"]?|['\"]?\]$", "", text)

        # Break comma loops
        if text.count(',') > 5:
            text = text.replace(',', ' ')
            text = re.sub(r'\s+', ' ', text)

        # Multi-word loop collapse
        words = text.split()
        if len(words) > 10:
            for n in range(1, 15):
                i = 0
                while i < len(words) - n * 2:
                    if words[i:i+n] == words[i+n:i+2*n]:
                        del words[i+n:i+2*n]
                        continue
                    i += 1
            text = " ".join(words)

        # Char-level loop collapse
        for length in range(20, min(250, len(text) // 2)):
            pattern = re.compile(r'(.{' + str(length) + r'})\1+')
            if pattern.search(text):
                text = pattern.sub(r'\1', text)

        return text.strip()

    def transcribe(self, audio_path: str, language: str = None) -> str:
        if not self.initialized:
            self.initialize()
            
        with self._lock:
            try:
                safe_print(f"🎙️ Transcribing {os.path.basename(audio_path)} (Lang: {language or 'auto'})...")
                
                qwen_lang = None
                if language:
                    lang_map = {
                        'en': 'English', 'ja': 'Japanese', 'de': 'German',
                        'zh': 'Chinese', 'ko': 'Korean', 'fr': 'French', 'auto': None
                    }
                    qwen_lang = lang_map.get(language.lower(), None)

                result = self.model.infer(audio_path, language=qwen_lang, return_route=False)
                safe_print("✅ Transcription segment complete")
                
                raw_text = ""
                if isinstance(result, list):
                    raw_text = " ".join([str(t).strip() for t in result if t])
                elif isinstance(result, dict) and "text" in result:
                    raw_text = result["text"]
                else:
                    raw_text = str(result)
                
                return self._deduplicate_phrases(raw_text)
            except Exception as e:
                # Log but don't crash the log pipe
                msg = str(e)
                if "Broken pipe" not in msg:
                    safe_print(f"❌ Transcription failed: {msg}")
                raise
