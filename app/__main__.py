import logging

import uvicorn

from app.main import create_app
from app.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = Settings()
uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, access_log=False)
