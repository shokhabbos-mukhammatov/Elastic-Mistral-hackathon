from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HG_", env_file=".env", extra="ignore")

    es_url: str = "http://localhost:9200"
    es_api_key: str = ""
    kibana_url: str = ""
    kibana_api_key: str = ""
    prefix: str = "hn-"

    mistral_api_key: str = ""
    model_large: str = "mistral-large-4"  # names per the hack night's mistral_guide.md
    model_small: str = "mistral-small-latest"
    model_embed: str = "mistral-embed"
    model_ocr: str = "mistral-ocr-latest"
    model_transcribe: str = "voxtral-mini-latest"
    model_tts: str = "voxtral-mini-tts-latest"
    model_moderation: str = "mistral-moderation-2603"

    @property
    def has_mistral(self) -> bool:
        return bool(self.mistral_api_key)

    def ix(self, name: str) -> str:
        return f"{self.prefix}{name}"


@lru_cache
def settings() -> Settings:
    return Settings()
