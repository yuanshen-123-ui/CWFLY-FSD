"""Configuration loader for FSD server.

Reads fsd.conf INI-style configuration files.
"""

import configparser
import os

DEFAULT_CONFIG = {
    "system": {
        "clientport": "6809",
        "serverport": "3011",
        "systemport": "3012",
        "password": "",
        "certificates": "cert.txt",
        "whazzup": "whazzup.txt",
        "maxclients": "500",
        "email": "fsd@localhost",
        "serverident": "",
        "servername": "FSD Python Server",
        "serveremail": "admin@localhost",
        "serverhost": "localhost",
        "serverlocation": "XX",
    },
    "connections": {
        "allowfrom": "",
    },
    "database": {
        "host": "127.0.0.1",
        "port": "3306",
        "name": "cwfly",
        "user": "root",
        "password": "password",
    },
    "weather": {
        "source": "download",
        "server": "weather.noaa.gov",
        "dir": "data/observations/metar/cycles/",
        "ftpmode": "passive",
    },
}


class Config:
    """Configuration wrapper around configparser."""

    def __init__(self, filename: str = "fsd.conf"):
        self.parser = configparser.ConfigParser()
        self.filename = filename
        self.loaded = False

    def load(self) -> bool:
        """Load configuration from file, fall back to defaults."""
        if os.path.exists(self.filename):
            # 优先按 UTF-8 读取，兼容含中文注释的配置（中文 Windows 下
            # 系统默认是 GBK，UTF-8 注释会导致 UnicodeDecodeError）；
            # 失败时回退到系统默认编码，兼容旧的非 UTF-8 配置。
            try:
                self.parser.read(self.filename, encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    self.parser.read(self.filename)
                except Exception:
                    return False
            self.loaded = True
        else:
            self._set_defaults()
        return self.loaded

    def _set_defaults(self):
        for section, options in DEFAULT_CONFIG.items():
            self.parser[section] = {}

    def get(self, section: str, key: str, default=None):
        """Get a config value as string."""
        try:
            return self.parser.get(section, key, fallback=default)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def getint(self, section: str, key: str, default: int = 0) -> int:
        val = self.get(section, key)
        if val is None:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    def getlist(self, section: str, key: str) -> list:
        """Get a colon-separated list of values."""
        val = self.get(section, key, "")
        if not val:
            return []
        return [v.strip() for v in val.split(":") if v.strip()]

    @property
    def client_port(self) -> int:
        return self.getint("system", "clientport", 6809)

    @property
    def server_port(self) -> int:
        return self.getint("system", "serverport", 3011)

    @property
    def system_port(self) -> int:
        return self.getint("system", "systemport", 3012)

    @property
    def system_password(self) -> str:
        return self.get("system", "password", "")

    @property
    def cert_file(self) -> str:
        return self.get("system", "certificates", "cert.txt")

    @property
    def whazzup_file(self) -> str:
        return self.get("system", "whazzup", "whazzup.txt")

    @property
    def max_clients(self) -> int:
        return self.getint("system", "maxclients", 500)

    @property
    def allow_from(self) -> list:
        return self.getlist("connections", "allowfrom")

    @property
    def connect_to(self) -> list:
        return self.getlist("connections", "connectto")

    @property
    def server_ident(self) -> str:
        return self.get("system", "serverident", "")

    @property
    def server_name(self) -> str:
        return self.get("system", "servername", "FSD Python Server")

    @property
    def server_email(self) -> str:
        return self.get("system", "serveremail", "admin@localhost")

    @property
    def server_host(self) -> str:
        return self.get("system", "serverhost", "localhost")

    @property
    def server_location(self) -> str:
        return self.get("system", "serverlocation", "XX")

    @property
    def weather_source(self) -> str:
        return self.get("weather", "source", "download")

    @property
    def weather_server(self) -> str:
        return self.get("weather", "server", "weather.noaa.gov")

    @property
    def weather_dir(self) -> str:
        return self.get("weather", "dir", "data/observations/metar/cycles/")
