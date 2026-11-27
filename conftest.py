import os

# Unconditionally isolate pytest and its child subprocesses from repo-local .env
# and inherited production storage/store env vars before any package __init__
# (such as chrome_engine/__init__.py) imports src.core.config.
os.environ["FOXPROFILE_IGNORE_DOTENV"] = "1"
for _var in (
    "FOXPROFILE_DATA_DIR",
    "FOXPROFILE_PROFILES_FILE",
    "FOXPROFILE_PROXY_POOL_FILE",
    "FOXPROFILE_WARMING_FILE",
    "FOXPROFILE_EXTENSIONS_FILE",
    "FOXPROFILE_EXTENSIONS_DIR",
    "FOXPROFILE_PREFERENCES_FILE",
    "FOXPROFILE_LOG_DIR",
    "FOXPROFILE_CORE_HOME",
    "FOXPROFILE_CORE_BACKUP_HOME",
):
    os.environ.pop(_var, None)
