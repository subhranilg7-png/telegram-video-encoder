#!/usr/bin/env python3
"""
Telegram Video Encoding Bot - Single File Version
Deploy on Render.com
"""

import os
import logging
import uuid
import asyncio
import tempfile
import threading
import time
import subprocess
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ==================== CONFIGURATION ====================
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
PORT = int(os.getenv('PORT', 8080))
MAX_FILE_SIZE = 1.6 * 1024 * 1024 * 1024  # 1.6GB

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Store active jobs
active_jobs = {}

# ==================== ENCODING CONFIG ====================
RESOLUTIONS = {
    '480p': '854:480',
    '720p': '1280:720',
    '1080p': '1920:1080'
}

CRF_VALUES = {
    'h264': {'480p': 24, '720p': 23, '1080p': 22},
    'h265': {'480p': 28, '720p': 27, '1080p': 26}
}

# ==================== HEALTH CHECK SERVER ====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass

def start_health_server():
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
        logger.info(f"✅ Health check server running on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Health server error: {e}")

health_thread = threading.Thread(target=start_health_server, daemon=True)
health_thread.start()

# ==================== ENCODING FUNCTIONS ====================
async def encode_video(input_path: str, quality: str, codec: str, job_id: str) -> str:
    if quality not in RESOLUTIONS:
        raise ValueError(f"Invalid quality: {quality}")
    if codec not in ['h264', 'h265']:
        raise ValueError(f"Invalid codec: {codec}")
    
    resolution = RESOLUTIONS[quality]
    crf = CRF_VALUES[codec][quality]
    
    temp_dir = "/tmp/encodes"
    os.makedirs(temp_dir, exist_ok=True)
    output_path = os.path.join(temp_dir, f"{job_id}_{quality}_{codec}.mkv")
    
    if codec == 'h265':
        cmd = [
            'ffmpeg', '-i', input_path,
            '-vf', f'scale={resolution}:flags=lanczos',
            '-c:v', 'libx265', '-crf', str(crf),
            '-preset', 'medium', '-tag:v', 'hvc1',
            '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
            '-f', 'matroska', '-threads', '1',
            '-max_muxing_queue_size', '1024', '-y',
            output_path
        ]
    else:
        cmd = [
            'ffmpeg', '-i', input_path,
            '-vf', f'scale={resolution}:flags=lanczos',
            '-c:v', 'libx264', '-crf', str(crf),
            '-preset', 'medium', '-tune', 'fastdecode',
            '-profile:v', 'main', '-level', '4.0',
            '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
            '-f', 'matroska', '-movflags', '+faststart',
            '-threads', '1', '-max_muxing_queue_size', '1024', '-y',
            output_path
        ]
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3600)
        
        if process.returncode != 0:
            error_msg = stderr.decode()[:500]
            raise Exception(f"FFmpeg error: {error_msg}")
        
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception("Output file is empty or missing")
        
        return output_path
    except Exception as e:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise e

# ==================== BOT COMMANDS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = """
🎥 *Video Encoding Bot*

*Send any video (up to 1.5GB)* and choose quality:

/480p - Encode to 480p
/720p - Encode to 720p  
/1080p - Encode to 1080p

*Commands:*
/codec - Switch H.264 ↔ H.265
/status - Check job status
/cancel - Cancel encoding
/help - Show instructions

*Format:* MKV
"""
    await update.message.reply_text(welcome, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📘 *How to Use:*

1. Send a video file
2. Choose quality: /480p /720p /1080p
3. Wait for encoding (5-20 min)
4. Download MKV file

⚡ *Tips:*
• /codec - Switch codec
• 1 job at a time
• Max 1.5GB
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    video = update.message.video
    
    if not video:
        await update.message.reply_text("❌ Please send a video file.")
        return
    
    if video.file_size > MAX_FILE_SIZE:
        size_mb = video.file_size / (1024 * 1024)
        await update.message.reply_text(f"❌ File too large! Max 1.5GB\nYour file: {size_mb:.1f}MB")
        return
    
    for jid, job in active_jobs.items():
        if job['user_id'] == user_id and job['status'] in ['downloading', 'encoding']:
            await update.message.reply_text("⏳ You already have a job in progress. Use /cancel")
            return
    
    job_id = str(uuid.uuid4())[:8]
    active_jobs[job_id] = {
        'user_id': user_id,
        'chat_id': update.effective_chat.id,
        'video': video,
        'status': 'waiting_for_quality',
        'codec': 'h264',
        'created_at': time.time()
    }
    
    file_size_mb = video.file_size / (1024 * 1024)
    await update.message.reply_text(
        f"✅ Video received!\n📊 Size: {file_size_mb:.1f}MB\n\n"
        f"Choose quality:\n/480p\n/720p\n/1080p\n\n"
        f"Codec: H.264 (use /codec to switch)",
        parse_mode='Markdown'
    )

async def encode_480p(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_encoding(update, '480p')

async def encode_720p(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_encoding(update, '720p')

async def encode_1080p(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_encoding(update, '1080p')

async def start_encoding(update: Update, quality: str):
    user_id = update.effective_user.id
    
    job_id = None
    job = None
    for jid, j in active_jobs.items():
        if j['user_id'] == user_id and j['status'] == 'waiting_for_quality':
            job_id = jid
            job = j
            break
    
    if not job_id:
        await update.message.reply_text("❌ No video pending! Send a video first.")
        return
    
    job['status'] = 'downloading'
    job['quality'] = quality
    
    msg = await update.message.reply_text(
        f"⏳ *Starting Encoding*\n\n📥 Downloading...\nQuality: {quality}\nCodec: {job['codec'].upper()}",
        parse_mode='Markdown'
    )
    
    asyncio.create_task(process_encoding_job(job_id, msg.message_id))

async def process_encoding_job(job_id: str, message_id: int):
    from telegram import Bot
    
    bot = Bot(token=TOKEN)
    job = active_jobs[job_id]
    
    temp_dir = None
    input_path = None
    output_path = None
    
    try:
        await bot.edit_message_text(
            chat_id=job['chat_id'], message_id=message_id,
            text="📥 *Downloading video...*", parse_mode='Markdown'
        )
        
        file = await job['video'].get_file()
        temp_dir = tempfile.mkdtemp(prefix=f"encode_{job_id}_")
        input_path = os.path.join(temp_dir, "input.mp4")
        await file.download_to_drive(input_path)
        
        await bot.edit_message_text(
            chat_id=job['chat_id'], message_id=message_id,
            text="🔧 *Encoding video...*\nThis may take 10-30 minutes",
            parse_mode='Markdown'
        )
        
        job['status'] = 'encoding'
        output_path = await encode_video(input_path, job['quality'], job['codec'], job_id)
        
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        
        with open(output_path, 'rb') as f:
            await bot.send_document(
                chat_id=job['chat_id'],
                document=f,
                filename=f"{job['quality']}_{job['codec']}.mkv",
                caption=f"✅ *Encoding Complete!*\n🎬 {job['quality']} • {job['codec'].upper()}\n📊 {file_size_mb:.1f}MB",
                parse_mode='Markdown'
            )
        
        await bot.edit_message_text(
            chat_id=job['chat_id'], message_id=message_id,
            text="✅ *Job Completed!*", parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Encoding failed: {e}")
        await bot.edit_message_text(
            chat_id=job['chat_id'], message_id=message_id,
            text=f"❌ *Failed!*\nError: {str(e)[:200]}", parse_mode='Markdown'
        )
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        if job_id in active_jobs:
            del active_jobs[job_id]

async def codec_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    for job in active_jobs.values():
        if job['user_id'] == user_id:
            new_codec = 'h265' if job['codec'] == 'h264' else 'h264'
            job['codec'] = new_codec
            await update.message.reply_text(f"🔄 Switched to *{new_codec.upper()}*", parse_mode='Markdown')
            return
    
    await update.message.reply_text("💡 Will use H.265 for your next video.", parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    for job_id, job in active_jobs.items():
        if job['user_id'] == user_id:
            status_text = {
                'waiting_for_quality': '⏳ Waiting for quality',
                'downloading': '📥 Downloading',
                'encoding': '🔧 Encoding'
            }.get(job['status'], job['status'])
            
            await update.message.reply_text(
                f"📊 *Status:* {status_text}\n"
                f"🎬 Quality: {job.get('quality', 'Not set')}\n"
                f"🎞️ Codec: {job['codec'].upper()}",
                parse_mode='Markdown'
            )
            return
    
    await update.message.reply_text("✅ No active jobs.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    for job_id, job in list(active_jobs.items()):
        if job['user_id'] == user_id:
            del active_jobs[job_id]
            await update.message.reply_text("✅ Job cancelled.")
            return
    
    await update.message.reply_text("❌ No job to cancel.")

# ==================== CLEANUP ====================
def cleanup_old_files():
    while True:
        try:
            temp_dir = "/tmp/encodes"
            if os.path.exists(temp_dir):
                now = time.time()
                for f in os.listdir(temp_dir):
                    path = os.path.join(temp_dir, f)
                    if os.path.isfile(path) and os.path.getmtime(path) < now - 3600:
                        os.remove(path)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        time.sleep(1800)

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

# ==================== MAIN ====================
def main():
    if not TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN not set!")
        return
    
    print(f"🤖 Starting bot...")
    os.makedirs("/tmp/encodes", exist_ok=True)
    
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("codec", codec_command))
    app.add_handler(CommandHandler("480p", encode_480p))
    app.add_handler(CommandHandler("720p", encode_720p))
    app.add_handler(CommandHandler("1080p", encode_1080p))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    
    RENDER_URL = os.getenv('RENDER_EXTERNAL_URL')
    
   # FIXED: Using webhook with proper package
print(f"🔗 Running on Render with webhook")
app.run_webhook(
    listen="0.0.0.0", 
    port=int(os.getenv('PORT', 8080)), 
    url_path="webhook", 
    webhook_url=f"{os.getenv('RENDER_EXTERNAL_URL')}/webhook"
)
