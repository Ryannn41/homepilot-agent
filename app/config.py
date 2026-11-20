from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 协议 v1 规定上下行音频统一为 16kHz、单声道、PCM16，不可配置
SAMPLE_RATE = 16000

DEFAULT_SYSTEM_PROMPT = (
    "你是家庭语音助手「小余」。你的回答会被转成语音播放，所以请用简短、自然的口语回答，"
    "一般不超过三句话；不要使用 Markdown、列表、表情符号或代码。"
    "你没有任何工具，不能执行命令、读写文件或上网，只能直接用说话来回答。"
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

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
