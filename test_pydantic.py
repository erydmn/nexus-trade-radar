import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr
from typing import Optional

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env_missing_test", extra="ignore")
    newsapi_key: Optional[SecretStr] = Field(default=None)

os.environ["FOO"] = "BAR"
print(Settings().model_dump())
