from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 协议 v1 规定上下行音频统一为 16kHz、单声道、PCM16，不可配置
SAMPLE_RATE = 16000

DEFAULT_SYSTEM_PROMPT = (
    "你是家庭语音助手「小余」。你的回答会被转成语音播放，所以请用简短、自然的口语回答，"
    "一般不超过三句话；不要使用 Markdown、列表、表情符号或代码。"
    "需要当前时间、天气或实时信息时，调用提供给你的工具获取，不要凭记忆编造；"
    "联网搜索比较慢，调用前先说一句「我查一下」之类的话。"
    "除了这些工具，你不能执行命令或读写文件。"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HOMEPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    auth_token: SecretStr = SecretStr("")  # 为空时不校验客户端令牌

    # 阿里云百炼 DashScope：ASR 与 TTS
    dashscope_api_key: SecretStr = SecretStr("")
    asr_model: str = "paraformer-realtime-v2"
    tts_model: str = "cosyvoice-v2"
    tts_voice: str = "longxiaochun_v2"

    # DeepSeek，经 Anthropic 兼容接口接入 Claude Agent SDK
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com/anthropic"
    deepseek_model: str = "deepseek-chat"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    # 工具
    timezone: str = "Asia/Shanghai"
    home_city: str = "上海"  # 用户没说地点时查询天气的城市
    search_model: str = "qwen-flash"  # 联网搜索用的通义千问模型（复用 DashScope key）

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
