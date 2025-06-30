# Meri Discord Bot

A powerful, multimodal Discord bot with AI chat capabilities, visual analysis, voice streaming, and web search functionality.

## Features

- **AI Chat** - Advanced conversations using local LLM models (LM Studio/Ollama)
- **Visual Analysis** - Analyze images, videos, GIFs, and YouTube content
- **Voice & Music** - Stream YouTube audio with queue management
- **Web Search** - Search the web and get AI-summarized results  
- **Content Summarization** - Summarize web pages, PDFs, and text files
- **Memory System** - Remembers conversation context per user

## Requirements

- Python 3.8+
- Discord.py 2.0+
- LM Studio or Ollama running locally
- FFmpeg (for video/audio processing)
- Optional: PyPDF2 or pypdf (for PDF processing)

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/Meri_Bot.git
cd Meri_Bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Install FFmpeg:
   - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html)
   - **macOS**: `brew install ffmpeg`
   - **Linux**: `sudo apt install ffmpeg`

4. Set up configuration:
   - Copy `Meri_Token.env.example` to `Meri_Token.env`
   - Add your Discord bot token and configure settings

5. Run the bot:
```bash
python Meri_Bot.py
```

## Configuration

All configuration is done through environment variables in `Meri_Token.env`. See `Meri_Token.env.example` for all available options.

### Required Configuration

- `TOKEN` - Your Discord bot token from [Discord Developer Portal](https://discord.com/developers/applications)

### Key Configuration Options

- `PREFIX` - Command prefix (default: `!`)
- `LMSTUDIO_CHAT_URL` - LM Studio/Ollama API endpoint
- `DEFAULT_CHAT_MODEL` - Default AI model for chat
- `DEFAULT_VISION_MODEL` - Default model for image/video analysis
- `MIN_VIDEO_FRAMES` - Number of frames to extract from videos

## Commands

### AI Chat Commands
- `!lm <prompt>` - Chat with AI (supports images/videos)
- `!lm -vis <prompt>` - Analyze video frames
- `!lm -m <prompt>` - Include replied message context
- `!lm -s <search> <prompt>` - Chat with web search context

### Search & Analysis
- `!search <query>` - Search web and get AI summary
- `!reason <prompt>` - AI chat with automatic web search
- `!sum <url>` - Summarize web content/YouTube videos
- `!vis <url/media>` - Comprehensive visual analysis

### Voice & Music
- `!join` - Join your voice channel
- `!play <url/search>` - Play YouTube audio
- `!skip` - Skip current song
- `!pause` / `!resume` - Control playback
- `!stop` - Stop and clear queue
- `!queue` - Show music queue
- `!volume <0-100>` - Set volume
- `!leave` - Leave voice channel

### Utility Commands
- `!help` - Show all commands
- `!help2` - Show advanced features
- `!clearcontext` - Clear your conversation memory

## Project Structure

```
Meri_Bot/
├── Meri_Bot.py         # Main bot file
├── config.py            # Configuration module
├── lm.py               # Language model commands
├── search.py           # Web search commands
├── reason.py           # Reasoning with search
├── sum.py              # Content summarization
├── vis.py              # Visual analysis
├── voice.py            # Voice commands
├── voice_handler.py    # Voice connection handler
├── requirements.txt    # Python dependencies
├── Meri_Token.env      # Your configuration (create this)
└── Meri_Token.env.example  # Configuration template
```

## Security Notes

- Never commit `Meri_Token.env` to version control
- Keep your bot token secret
- Review permissions before inviting bot to servers
- Log files may contain sensitive information

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

This project is for educational and personal use. Please respect Discord's Terms of Service and rate limits.

## Troubleshooting

### Bot won't start
- Check your bot token is correct in `Meri_Token.env`
- Ensure LM Studio/Ollama is running
- Check Python version (3.8+ required)

### Voice commands not working
- Ensure FFmpeg is installed and in PATH
- Check bot has voice permissions in Discord
- Try `!voice4006fix` if getting session errors

### Visual analysis failing
- Install Pillow: `pip install Pillow`
- Ensure FFmpeg is available for video processing
- Check file size limits in configuration 