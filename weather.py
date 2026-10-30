"""Weather/METAR management.

多源 METAR 拉取（2026-10-30 重写，原实现单一依赖 NOAA）：

  Source 1  aviationweather.gov  —— NOAA 全球 METAR（主源）
  Source 2  metar.vatsim.net     —— VATSIM 聚合（备用；与 NOAA 同源，主要价值是单源故障时的韧性）
  Source 3  metar-taf.com        —— 可选，默认关闭：实测 Cloudflare 对服务器 IP 全量 403，
                                    需出口被放行后设 WEATHER_ENABLE_METAR_TAF=1 才启用

站点清单由 **OurAirports**（Public Domain）自动生成：中国+港澳台的中大型机场 4 字码，
本地缓存 data/airports_cn.json（7 天 TTL），下载失败回退旧缓存/内置清单。

输出 metar.dat：每行 `<STATION> DDHHMMZ ...`（去掉 'METAR ' 前缀，兼容历史格式与 get_metar），
原子写入（临时文件 + os.replace），避免客户端读到半截文件。

说明（2026-10-30 实测）：ZBLA/ZBMZ/ZBTL 等国内支线机场在 NOAA/VATSIM 公开数据流中**没有发布 METAR**，
任何多源组合都无法凭空产生这些报文；本模块的作用是「不丢已有数据 + 单源故障可降级 + 覆盖情况可观测」。
"""

import asyncio
import csv
import io
import logging
import os
import time
import urllib.request
from typing import Dict, List, Optional

logger = logging.getLogger("fsd.weather")

# Max METAR file size (5MB)
MAX_METAR_SIZE = 5 * 1024 * 1024

AWC_URL = "https://aviationweather.gov/api/data/metar?ids={ids}&format=raw"
VATSIM_URL = "https://metar.vatsim.net/metar.php?id={ids}"
METAR_TAF_URL = "https://metar-taf.com/metar/{icao}"

# OurAirports 机场库（Public Domain）
OURAIRPORTS_CSV = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OURAIRPORTS_MAX_BYTES = 40 * 1024 * 1024
OURAIRPORTS_TTL = 7 * 24 * 3600
OURAIRPORTS_COUNTRIES = {"CN", "HK", "MO", "TW"}
OURAIRPORTS_TYPES = {"large_airport", "medium_airport"}

# 批量查询上限（单次 URL 不要过长，实测 57 个可一次查完，这里取 40 保守分片）
BATCH_SIZE = 40
SRC_TIMEOUT = 25

_HERE = os.path.dirname(os.path.abspath(__file__))
_METAR_PATH = os.path.join(_HERE, "metar.dat")
_STATIONS_CACHE = os.path.join(_HERE, "data", "airports_cn.json")

# 内置兜底清单（网络不可用且无缓存时使用；与历史硬编码清单保持一致）
FALLBACK_STATIONS = [
    "ZBAA", "ZBAD", "ZBLA", "ZBHH", "ZBSJ", "ZBTL", "ZBYC", "ZBYN", "ZBMZ",
    "ZGYL", "ZGHA", "ZGCL", "ZGGG", "ZGOW", "ZGSD", "ZGSY", "ZHHH", "ZHLY",
    "ZHNY", "ZHSN", "ZJQH", "ZLIC", "ZLXY", "ZLSN", "ZLZH", "ZLXN", "ZLYA",
    "ZPKX", "ZPPP", "ZPMS", "ZSQZ", "ZSSS", "ZSFZ", "ZSHC", "ZSWX", "ZSWZ",
    "ZSCN", "ZSTN", "ZSYA", "ZSZS", "ZUUU", "ZUYB", "ZULZ", "ZUXC", "ZUTZ",
    "ZWAT", "ZWKM", "ZWYN", "ZYCC", "ZYHB", "ZYDD", "ZYTL", "ZYYJ",
    "VHHH", "VMMC", "RCTP", "RCSS",
]


def _http_get(url: str, timeout: int = SRC_TIMEOUT, max_bytes: int = MAX_METAR_SIZE) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "fsd-py/1.0 (+cwflysim.cn)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise MemoryError("download exceeded size limit")
    return data


def _chunks(seq: List[str], n: int) -> List[List[str]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _parse_station_lines(text: str) -> Dict[str, str]:
    """把 'METAR ZBAA 300800Z ...' / 'ZBAA 300800Z ...' 解析为 {ICAO: 'ICAO DDHHMMZ ...'}"""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 去掉 METAR/SPECI 前缀，统一成「站点优先」格式
        line = line.replace("METAR ", "", 1) if line.startswith("METAR ") else line
        line = line.replace("SPECI ", "", 1) if line.startswith("SPECI ") else line
        parts = line.split()
        if len(parts) < 2:
            continue
        icao = parts[0].upper()
        if len(icao) != 4 or not icao.isalpha():
            continue
        if not parts[1].endswith("Z"):
            continue
        out[icao] = " ".join([icao] + parts[1:])
    return out


def _load_station_list() -> List[str]:
    """OurAirports → CN/HK/MO/TW 中大型机场 4 字码；失败回退缓存/内置清单。"""
    # 1) 新鲜缓存
    try:
        if os.path.exists(_STATIONS_CACHE) and (time.time() - os.path.getmtime(_STATIONS_CACHE)) < OURAIRPORTS_TTL:
            import json
            with open(_STATIONS_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
            stations = data.get("stations") or []
            if len(stations) >= 50:
                logger.info(f"METAR station list from cache: {len(stations)} airports")
                return stations
    except Exception as e:
        logger.warning(f"读站点缓存失败: {e}")

    # 2) 下载并解析
    try:
        raw = _http_get(OURAIRPORTS_CSV, timeout=90, max_bytes=OURAIRPORTS_MAX_BYTES)
        rows = csv.reader(io.StringIO(raw.decode("utf8", "replace")))
        header = next(rows)
        idx = {name: i for i, name in enumerate(header)}
        need = ("ident", "type", "iso_country", "icao_code", "gps_code", "local_code")
        if not all(k in idx for k in need):
            raise ValueError(f"CSV 缺列: {[k for k in need if k not in idx]}")
        found = set()
        for row in rows:
            if len(row) <= max(idx.values()):
                continue
            if row[idx["iso_country"]] not in OURAIRPORTS_COUNTRIES:
                continue
            if row[idx["type"]] not in OURAIRPORTS_TYPES:
                continue
            for key in ("icao_code", "ident", "gps_code", "local_code"):
                v = (row[idx[key]] or "").strip().upper()
                if len(v) == 4 and v.isalpha():
                    found.add(v)
                    break
        stations = sorted(found)
        if len(stations) < 50:
            raise ValueError(f"解析到的站点过少: {len(stations)}")
        try:
            import json
            os.makedirs(os.path.dirname(_STATIONS_CACHE), exist_ok=True)
            tmp = _STATIONS_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "source": OURAIRPORTS_CSV, "count": len(stations),
                           "stations": stations}, f)
            os.replace(tmp, _STATIONS_CACHE)
        except Exception as e:
            logger.warning(f"写站点缓存失败: {e}")
        logger.info(f"METAR station list updated from OurAirports: {len(stations)} airports")
        return stations
    except Exception as e:
        logger.warning(f"OurAirports 更新失败，回退内置清单: {e}")

    # 3) 陈旧缓存 → 4) 内置清单
    try:
        import json
        with open(_STATIONS_CACHE, "r", encoding="utf-8") as f:
            stations = (json.load(f).get("stations") or [])
        if len(stations) >= 50:
            return stations
    except Exception:
        pass
    return list(FALLBACK_STATIONS)


def _fetch_aviationweather(stations: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for chunk in _chunks(stations, BATCH_SIZE):
        text = _http_get(AWC_URL.format(ids=",".join(chunk))).decode("utf8", "replace")
        out.update(_parse_station_lines(text))
    return out


def _fetch_vatsim(stations: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for chunk in _chunks(stations, BATCH_SIZE):
        text = _http_get(VATSIM_URL.format(ids=",".join(chunk))).decode("utf8", "replace")
        out.update(_parse_station_lines(text))
    return out


def _fetch_metar_taf(stations: List[str], limit: int = 20) -> Dict[str, str]:
    """可选源：逐站抓取（默认关闭；被 Cloudflare 拦时快速失败）"""
    out: Dict[str, str] = {}
    for icao in stations[:limit]:
        try:
            html = _http_get(METAR_TAF_URL.format(icao=icao), timeout=10, max_bytes=2 * 1024 * 1024).decode("utf8", "replace")
            import re
            m = re.search(r"METAR\s+%s\s+\d{6}Z[^<\n]{0,200}" % icao, html)
            if m:
                parsed = _parse_station_lines(m.group(0))
                out.update(parsed)
        except Exception:
            continue
    return out


class WeatherManager:
    """Manages METAR weather data downloads."""

    def __init__(self, server):
        self.server = server
        self.config = server.config
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._prev_hour = -1
        self._prev_download = 0
        self.last_report: Dict[str, object] = {}

    def start(self):
        """Start the weather update loop."""
        if self.config.weather_source != "download":
            logger.info("Weather source is not download, skipping METAR fetch")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Weather manager started (multi-source: aviationweather -> vatsim"
                    + (", metar-taf" if os.environ.get("WEATHER_ENABLE_METAR_TAF") == "1" else "")
                    + ")")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run_loop(self):
        """Main weather update loop - checks every 60 seconds."""
        while self._running:
            try:
                await self._check_update()
            except Exception as e:
                logger.error(f"Weather check error: {e}")
            await asyncio.sleep(60)

    async def _check_update(self):
        """Check if a new METAR download is needed."""
        now = time.time()
        tm = time.gmtime(now)

        # Download at 00/12 UTC, or if more than 1 hour passed
        if (self._prev_hour == -1 or
            (self._prev_hour != tm.tm_hour and tm.tm_hour in (0, 12)) or
            (now - self._prev_download) > 3600):

            self._prev_hour = tm.tm_hour
            await self._download_metar()

    async def _download_metar(self):
        """多源拉取并合并 METAR（任一路径失败不影响其它源）。"""
        if not self.config.weather_server:
            logger.warning("No METAR host configured")
            return

        def work():
            from concurrent.futures import ThreadPoolExecutor, as_completed

            stations = _load_station_list()
            merged: Dict[str, str] = {}
            report: Dict[str, object] = {"stations": len(stations), "sources": {}}

            jobs = {"aviationweather": lambda: _fetch_aviationweather(stations),
                    "vatsim": lambda: _fetch_vatsim(stations)}
            if os.environ.get("WEATHER_ENABLE_METAR_TAF") == "1":
                jobs["metar_taf"] = lambda: _fetch_metar_taf(stations)

            # 各源并行抓取（单源异常隔离）
            with ThreadPoolExecutor(max_workers=3) as pool:
                futs = {pool.submit(fn): name for name, fn in jobs.items()}
                for fut in as_completed(futs):
                    name = futs[fut]
                    try:
                        data = fut.result()
                    except Exception as e:
                        logger.warning(f"METAR source {name} failed: {e}")
                        report["sources"][name] = {"error": str(e)[:120]}
                        continue
                    added = 0
                    for icao, line in data.items():
                        if icao not in merged:
                            merged[icao] = line
                            added += 1
                    report["sources"][name] = {"got": len(data), "added": added}

            if not merged:
                raise RuntimeError("所有 METAR 源均无数据")

            body = "\n".join(merged[k] for k in sorted(merged)) + "\n"
            raw = body.encode("utf8")
            if len(raw) > MAX_METAR_SIZE:
                raise MemoryError("METAR 合并结果超过 5MB 上限")

            # 原子写入
            tmp = _METAR_PATH + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, _METAR_PATH)

            report["merged"] = len(merged)
            report["bytes"] = len(raw)
            report["missing"] = len(stations) - len([s for s in stations if s in merged])
            return report

        try:
            logger.info("Downloading METAR (multi-source)")
            loop = asyncio.get_event_loop()
            report = await loop.run_in_executor(None, work)
            self._prev_download = time.time()
            self.last_report = report
            src_desc = " ".join(f"{k}={v.get('got', 0)}" if isinstance(v, dict) else f"{k}=0"
                                for k, v in (report.get("sources") or {}).items())
            logger.info(f"METAR merged: {src_desc} | total={report.get('merged')}/{report.get('stations')} "
                        f"missing={report.get('missing')} ({report.get('bytes')} bytes)")
            if report.get("missing", 0) > 0:
                logger.info(f"METAR 未覆盖 {report.get('missing')} 个机场（多为国内支线机场，"
                            f"未在 NOAA/VATSIM 公开数据流中发布）")
        except Exception as e:
            logger.error(f"METAR download failed: {e}")

    def get_metar(self, station: str) -> Optional[str]:
        """Get METAR for a specific station.

        修复(2026-10-30)：原实现 `line.startswith(station)` 无法匹配 aviationweather 的
        'METAR ZBAA ...' 前缀格式（导致 get_metar 恒返回 None）。现按首 token 匹配，兼容两种格式。
        """
        code = (station or "").strip().upper()
        if not code:
            return None
        try:
            with open(_METAR_PATH, "r", encoding="utf8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    first = s.split(None, 1)[0]
                    if first == code or (first in ("METAR", "SPECI") and s.split(None, 2)[1:2] == [code]):
                        return s
        except FileNotFoundError:
            pass
        return None
