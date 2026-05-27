// audio/transcription/mega_asr_provider.rs
//
// Mega-ASR transcription provider implementation via local Python backend.

use super::provider::{TranscriptionError, TranscriptionProvider, TranscriptResult};
use async_trait::async_trait;
use log::{info, error, debug, warn};
use reqwest::multipart;
use serde::Deserialize;
use tauri::{AppHandle, Manager, Runtime, Emitter};
use std::path::PathBuf;

#[derive(Deserialize)]
struct MegaAsrResponse {
    text: String,
    // confidence: Option<f32>,
}

/// Mega-ASR transcription provider (calls local Python backend)
pub struct MegaAsrProvider<R: Runtime> {
    app: AppHandle<R>,
    client: reqwest::Client,
    endpoint: String,
}

impl<R: Runtime> MegaAsrProvider<R> {
    pub fn new(app: AppHandle<R>) -> Self {
        Self {
            app,
            client: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(300)) // 5 minute timeout for foundation model
                .build()
                .unwrap_or_default(),
            endpoint: "http://127.0.0.1:5167/transcribe".to_string(),
        }
    }

    /// Resolve the backend directory path (works for both dev and bundled app)
    fn get_backend_dir(&self) -> Option<PathBuf> {
        // 1. Try resolving via Tauri's resource directory (for bundled app)
        if let Ok(resource_dir) = self.app.path().resource_dir() {
            // Check normalized path (from resource mapping)
            let normalized_path = resource_dir.join("backend/app");
            if normalized_path.exists() {
                info!("Found backend in normalized resource path: {:?}", normalized_path);
                return Some(normalized_path);
            }

            // Check Tauri's default _up_ mapping for parent directories
            let up_path = resource_dir.join("_up_/_up_/backend/app");
            if up_path.exists() {
                info!("Found backend in _up_ resource path: {:?}", up_path);
                return Some(up_path);
            }
        }

        // 2. Fallback to current directory (for development)
        if let Ok(cwd) = std::env::current_dir() {
            let dev_path = cwd.join("backend/app");
            if dev_path.exists() {
                info!("Found backend in development path: {:?}", dev_path);
                return Some(dev_path);
            }
        }

        // 3. Fallback to sibling of executable (sometimes used in bundled apps)
        if let Ok(exe_path) = std::env::current_exe() {
            if let Some(exe_dir) = exe_path.parent() {
                let sibling_path = exe_dir.join("backend/app");
                if sibling_path.exists() {
                    info!("Found backend in sibling path: {:?}", sibling_path);
                    return Some(sibling_path);
                }
            }
        }

        None
    }

    /// Check if the Python backend is running, and start it if necessary
    async fn ensure_backend_running(&self) -> Result<(), String> {
        // First check if already running
        match self.client.get("http://127.0.0.1:5167/health").send().await {
            Ok(resp) if resp.status().is_success() => {
                debug!("Mega-ASR backend is already running.");
                return Ok(());
            }
            _ => {
                info!("Mega-ASR backend not responding, attempting to start it...");
            }
        }

        let backend_dir = self.get_backend_dir();
        let dir = backend_dir.ok_or_else(|| "Backend directory not found. Please ensure the 'backend' folder is in the application root or resources.".to_string())?;

        // Determine the Python executable path
        // Priority: 1. .venv/bin/python inside the resources, 2. .venv/bin/python in dev dir, 3. system python3
        let python_exe = {
            let mut exe_path = None;
            
            // Check for venv sibling to the 'app' directory (standard layout)
            if let Some(parent) = dir.parent() {
                let venv_path = if cfg!(target_os = "windows") {
                    parent.join(".venv/Scripts/python.exe")
                } else {
                    parent.join(".venv/bin/python")
                };
                
                if venv_path.exists() {
                    info!("Found virtual environment at: {:?}", venv_path);
                    exe_path = Some(venv_path.to_string_lossy().to_string());
                }
            }
            
            exe_path.unwrap_or_else(|| {
                if cfg!(target_os = "windows") {
                    "python".to_string()
                } else {
                    "python3".to_string()
                }
            })
        };

        info!("Spawning Python backend using: {} in {:?}", python_exe, dir);
        let mut child = std::process::Command::new(python_exe)
            .arg("main.py")
            .current_dir(&dir)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .map_err(|e| format!("Failed to spawn Python backend: {}", e))?;

        // Spawn threads to capture stdout/stderr and emit as logs
        if let Some(stdout) = child.stdout.take() {
            let app_handle = self.app.clone();
            std::thread::spawn(move || {
                use std::io::{BufRead, BufReader};
                let reader = std::io::BufReader::new(stdout);
                for line in reader.lines() {
                    if let Ok(l) = line {
                        let _ = app_handle.emit("backend-log", format!("[PY-OUT] {}", l));
                    }
                }
            });
        }

        if let Some(stderr) = child.stderr.take() {
            let app_handle = self.app.clone();
            std::thread::spawn(move || {
                use std::io::{BufRead, BufReader};
                let reader = std::io::BufReader::new(stderr);
                for line in reader.lines() {
                    if let Ok(l) = line {
                        let _ = app_handle.emit("backend-log", format!("[PY-ERR] {}", l));
                    }
                }
            });
        }

        // Wait a bit for the backend to start (increased to 10s for foundation model loading)
        let _ = self.app.emit("backend-log", "⏳ Spawning Python backend... (First load may take up to 30s)");
        tokio::time::sleep(std::time::Duration::from_secs(10)).await;

        // Final health check
        let mut retry_count = 0;
        let max_retries = 5;
        loop {
            match self.client.get("http://127.0.0.1:5167/health").send().await {
                Ok(resp) if resp.status().is_success() => {
                    info!("✅ Mega-ASR backend started successfully.");
                    let _ = self.app.emit("backend-log", "✅ Backend responded to health check.");
                    break;
                }
                _ => {
                    if retry_count >= max_retries {
                        return Err("Failed to connect to Mega-ASR backend after spawning. Ensure dependencies are installed in backend/.venv".to_string());
                    }
                    retry_count += 1;
                    let _ = self.app.emit("backend-log", format!("⏳ Waiting for backend health check (retry {}/{})...", retry_count, max_retries));
                    tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                }
            }
        }
        Ok(())
        }

    /// Manual WAV creation (PCM 16-bit, 16kHz, Mono)
    fn create_wav_data(samples: &[f32], sample_rate: u32) -> Vec<u8> {
        let n_samples = samples.len();
        let data_size = n_samples * 2; // 16-bit PCM
        let file_size = 36 + data_size;

        let mut wav = Vec::with_capacity(44 + data_size);

        // RIFF header
        wav.extend_from_slice(b"RIFF");
        wav.extend_from_slice(&(file_size as u32).to_le_bytes());
        wav.extend_from_slice(b"WAVE");

        // fmt chunk
        wav.extend_from_slice(b"fmt ");
        wav.extend_from_slice(&16u32.to_le_bytes()); // chunk size
        wav.extend_from_slice(&1u16.to_le_bytes());  // PCM format
        wav.extend_from_slice(&1u16.to_le_bytes());  // Mono
        wav.extend_from_slice(&sample_rate.to_le_bytes());
        wav.extend_from_slice(&(sample_rate * 2).to_le_bytes()); // Byte rate (rate * channels * bit_depth/8)
        wav.extend_from_slice(&2u16.to_le_bytes()); // Block align (channels * bit_depth/8)
        wav.extend_from_slice(&16u16.to_le_bytes()); // Bit depth

        // data chunk
        wav.extend_from_slice(b"data");
        wav.extend_from_slice(&(data_size as u32).to_le_bytes());

        // PCM samples (convert f32 to i16)
        for &sample in samples {
            let pcm = (sample.clamp(-1.0, 1.0) * 32767.0) as i16;
            wav.extend_from_slice(&pcm.to_le_bytes());
        }

        wav
    }
}

#[async_trait]
impl<R: Runtime> TranscriptionProvider for MegaAsrProvider<R> {
    async fn transcribe(
        &self,
        audio: Vec<f32>,
        language: Option<String>,
    ) -> std::result::Result<TranscriptResult, TranscriptionError> {
        // Ensure backend is running before transcribing
        let _ = self.app.emit("backend-log", "Checking Mega-ASR backend status...");
        if let Err(e) = self.ensure_backend_running().await {
            error!("❌ Backend management error: {}", e);
            let _ = self.app.emit("backend-log", format!("❌ Backend error: {}", e));
            return Err(TranscriptionError::EngineFailed(e));
        }

        info!("🚀 Sending audio to Mega-ASR local backend ({} samples)", audio.len());
        let _ = self.app.emit("backend-log", format!("🚀 Sending {} audio samples to model...", audio.len()));

        // Prepare WAV data
        let wav_data = Self::create_wav_data(&audio, 16000); // Assuming 16kHz for STT

        // Prepare multipart form
        let mut form = multipart::Form::new()
            .part("file", multipart::Part::bytes(wav_data).file_name("audio.wav").mime_str("audio/wav").unwrap())
            .text("model", "mega-asr");

        if let Some(lang) = language {
            form = form.text("language", lang);
        }

        // Send request
        let _ = self.app.emit("backend-log", "⏳ Waiting for AI inference (this may take a while for large segments)...");
        let response = self.client.post(&self.endpoint)
            .multipart(form)
            .send()
            .await
            .map_err(|e| {
                error!("❌ Failed to connect to Mega-ASR backend: {}", e);
                let _ = self.app.emit("backend-log", format!("❌ Connection failed: {}", e));
                TranscriptionError::EngineFailed(format!("Local Python backend unreachable: {}", e))
            })?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            error!("❌ Mega-ASR backend returned error {}: {}", status, error_text);
            let _ = self.app.emit("backend-log", format!("❌ Backend error {}: {}", status, error_text));
            return Err(TranscriptionError::EngineFailed(format!("Backend error {}: {}", status, error_text)));
        }

        let _ = self.app.emit("backend-log", "✅ Inference complete, parsing results...");
        let mega_resp: MegaAsrResponse = response.json().await.map_err(|e| {
            error!("❌ Failed to parse Mega-ASR response: {}", e);
            TranscriptionError::EngineFailed(format!("Invalid response from backend: {}", e))
        })?;

        debug!("✅ Received transcript from Mega-ASR: {}", mega_resp.text);

        Ok(TranscriptResult {
            text: mega_resp.text,
            confidence: None,
            is_partial: false,
        })
    }

    async fn is_model_loaded(&self) -> bool {
        // We assume the backend is ready if it responds to health check
        // For simplicity, we just return true here as the backend handles loading
        true
    }

    async fn get_current_model(&self) -> Option<String> {
        Some("Mega-ASR (Local)".to_string())
    }

    fn provider_name(&self) -> &'static str {
        "Mega-ASR"
    }
}
