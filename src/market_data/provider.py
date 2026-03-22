"""
市場資料層

職責：
- 根據 symbol + 時間區間抓取 K 線
- 支援多資料源：Binance（加密貨幣）、yfinance（外匯/CFD）
- K 線快取到 DB，避免重複抓取
- Symbol 映射（頻道名稱 → 交易所代號）
- 自動偵測 symbol 類型選擇資料源

輸入：symbol, timeframe, start_time, end_time
輸出：list[Candle]
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from src.models import Candle, CandleORM

logger = logging.getLogger(__name__)


# ============================================================
# Symbol 映射
# ============================================================

DEFAULT_SYMBOL_MAP: dict[str, str] = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
    "BTCUSDT": "BTC/USDT",
    "ETHUSDT": "ETH/USDT",
    "BNBUSDT": "BNB/USDT",
    "SOLUSDT": "SOL/USDT",
    "SOL": "SOL/USDT",
    "BNB": "BNB/USDT",
    "XRP": "XRP/USDT",
    "XRPUSDT": "XRP/USDT",
    "DOGEUSDT": "DOGE/USDT",
    "DOGE": "DOGE/USDT",
}

# yfinance 用的 symbol
YFINANCE_SYMBOLS: dict[str, str] = {
    "NAS100USD": "NQ=F",
    "XAUUSD": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "XAGUSD": "SI=F",
}


def normalize_symbol(symbol: str, custom_map: Optional[dict] = None) -> str:
    symbol = symbol.upper().strip()

    # 移除 .P 後綴（Binance 永續標記）
    if symbol.endswith(".P"):
        symbol = symbol[:-2]

    mapping = {**DEFAULT_SYMBOL_MAP, **(custom_map or {})}

    if symbol in mapping:
        return mapping[symbol]

    # XXXUSDT → XXX/USDT（Binance 格式）
    if symbol.endswith("USDT") and "/" not in symbol:
        base = symbol[:-4]
        return f"{base}/USDT"

    return symbol


def is_yfinance_symbol(symbol: str) -> bool:
    """判斷是否應該用 yfinance 抓資料（已改用 tvDatafeed 抓 OANDA）"""
    return False  # 不再用 yfinance，外匯改用 OandaTVProvider


def is_oanda_symbol(symbol: str) -> bool:
    """判斷是否為 OANDA 商品（外匯/CFD）"""
    return symbol in YFINANCE_SYMBOLS or not symbol.endswith("USDT")


# ============================================================
# 資料源介面
# ============================================================

class MarketDataProvider(ABC):
    @abstractmethod
    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        since: datetime,
        until: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[Candle]:
        ...


class BinanceProvider(MarketDataProvider):
    """Binance K 線資料提供者（使用 ccxt）"""

    def __init__(self):
        import ccxt
        self.exchange = ccxt.binance({"enableRateLimit": True})

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "15m",
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[Candle]:
        since_ts = int(since.timestamp() * 1000) if since else None

        all_candles = []
        fetched = 0

        while True:
            ohlcv = self.exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since_ts, limit=min(limit - fetched, 1000)
            )
            if not ohlcv:
                break

            for row in ohlcv:
                ts, o, h, l, c, v = row
                candle_time = datetime.fromtimestamp(ts / 1000, tz=__import__('datetime').timezone.utc).replace(tzinfo=None)

                if until and candle_time > until:
                    return all_candles

                all_candles.append(Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=candle_time,
                    open=o, high=h, low=l, close=c, volume=v,
                ))

            fetched += len(ohlcv)
            if fetched >= limit or len(ohlcv) < 1000:
                break

            since_ts = ohlcv[-1][0] + 1

        logger.info(f"[Binance] 抓取 {symbol} {timeframe} K線 {len(all_candles)} 根")
        return all_candles


class YFinanceProvider(MarketDataProvider):
    """yfinance K 線資料提供者（外匯 / CFD）"""

    # yfinance interval 映射
    TF_MAP = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "4h": "1h",  # yfinance 沒有 4h，用 1h 再合併
        "1d": "1d",
    }

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[Candle]:
        import yfinance as yf

        yf_interval = self.TF_MAP.get(timeframe, "1h")
        need_resample = (timeframe == "4h" and yf_interval == "1h")

        # yfinance 對 intraday 有天數限制：
        # 1m → 7 天, 5m/15m/30m → 60 天, 1h → 730 天
        start_str = since.strftime("%Y-%m-%d") if since else None
        end_str = (until + timedelta(days=1)).strftime("%Y-%m-%d") if until else None

        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_str, end=end_str, interval=yf_interval)

        if df.empty:
            logger.warning(f"[yfinance] 無資料: {symbol} {timeframe}")
            return []

        # 4h 重新取樣
        if need_resample:
            df = df.resample("4h").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()

        candles = []
        for idx, row in df.iterrows():
            open_time = idx.to_pydatetime().replace(tzinfo=None)

            if since and open_time < since:
                continue
            if until and open_time > until:
                break

            candles.append(Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row.get("Volume", 0)),
            ))

        logger.info(f"[yfinance] 抓取 {symbol} {timeframe} K線 {len(candles)} 根")
        return candles


class OandaTVProvider(MarketDataProvider):
    """從 TradingView 抓 OANDA 報價（外匯 / CFD）"""

    # OANDA symbol 映射
    OANDA_MAP = {
        "NAS100USD": "NAS100USD",
        "XAUUSD": "XAUUSD",
        "XAGUSD": "XAGUSD",
        "EURUSD": "EURUSD",
        "GBPUSD": "GBPUSD",
        "USDJPY": "USDJPY",
        "EURJPY": "EURJPY",
        "GBPJPY": "GBPJPY",
        "AUDUSD": "AUDUSD",
        "NZDUSD": "NZDUSD",
        "USDCHF": "USDCHF",
        "USDCAD": "USDCAD",
    }

    TF_MAP = {
        "1m": None, "5m": None, "15m": None, "30m": None,
        "1h": None, "4h": None, "1d": None,
    }

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[Candle]:
        from tvDatafeed import TvDatafeed, Interval

        tf_map = {
            "1m": Interval.in_1_minute, "5m": Interval.in_5_minute,
            "15m": Interval.in_15_minute, "30m": Interval.in_30_minute,
            "1h": Interval.in_1_hour, "4h": Interval.in_4_hour,
            "1d": Interval.in_daily,
        }

        interval = tf_map.get(timeframe, Interval.in_1_hour)

        # 找 OANDA symbol
        oanda_sym = symbol
        for k, v in self.OANDA_MAP.items():
            if k in symbol.upper().replace("/", "").replace("-", "").replace("=X", "").replace("=F", ""):
                oanda_sym = v
                break

        try:
            tv = TvDatafeed()
            df = tv.get_hist(symbol=oanda_sym, exchange="OANDA", interval=interval, n_bars=min(limit, 5000))

            if df is None or df.empty:
                logger.warning(f"[OANDA/TV] 無資料: {oanda_sym} {timeframe}")
                return []

            candles = []
            for idx, row in df.iterrows():
                open_time = idx.to_pydatetime().replace(tzinfo=None)

                if since and open_time < since:
                    continue
                if until and open_time > until:
                    break

                candles.append(Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=open_time,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                ))

            logger.info(f"[OANDA/TV] 抓取 {oanda_sym} {timeframe} K線 {len(candles)} 根")
            return candles

        except Exception as e:
            logger.error(f"[OANDA/TV] 抓取失敗 {oanda_sym}: {e}")
            return []


# ============================================================
# 自動選擇資料源
# ============================================================

class AutoProvider(MarketDataProvider):
    """根據 symbol 自動選擇 Binance 或 OANDA/TV"""

    def __init__(self):
        self._binance = None
        self._oanda_tv = None

    def _get_binance(self) -> BinanceProvider:
        if self._binance is None:
            self._binance = BinanceProvider()
        return self._binance

    def _get_oanda_tv(self) -> OandaTVProvider:
        if self._oanda_tv is None:
            self._oanda_tv = OandaTVProvider()
        return self._oanda_tv

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[Candle]:
        # 加密 → Binance, 外匯/CFD → OANDA via TradingView
        if symbol.endswith("USDT") or "/" in symbol:
            return self._get_binance().fetch_candles(symbol, timeframe, since, until, limit)
        else:
            return self._get_oanda_tv().fetch_candles(symbol, timeframe, since, until, limit)


# ============================================================
# 帶快取的資料載入
# ============================================================

def load_candles(
    session: Session,
    symbol: str,
    timeframe: str,
    since: datetime,
    until: datetime,
    provider: Optional[MarketDataProvider] = None,
    symbol_map: Optional[dict] = None,
) -> list[Candle]:
    """
    載入 K 線資料，優先從 DB 快取讀取，不足再從 API 抓取。
    自動根據 symbol 選擇資料源（Binance 或 yfinance）。
    """
    normalized = normalize_symbol(symbol, symbol_map)

    # 先查 DB
    cached = (
        session.query(CandleORM)
        .filter(
            CandleORM.symbol == normalized,
            CandleORM.timeframe == timeframe,
            CandleORM.open_time >= since,
            CandleORM.open_time <= until,
        )
        .order_by(CandleORM.open_time.asc())
        .all()
    )

    if cached:
        # NOTE: cache may be incomplete, but any cached results are acceptable for now.
        # If fewer than expected, consider fetching from API in a future improvement.
        logger.debug(f"從快取讀取 {len(cached)} 根 K 線")
        return [
            Candle(
                symbol=c.symbol, timeframe=c.timeframe, open_time=c.open_time,
                open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume or 0,
            )
            for c in cached
        ]

    # DB 沒有，從 API 抓
    if provider is None:
        provider = AutoProvider()

    source_name = "oanda_tv" if is_oanda_symbol(normalized) else "binance"

    candles = provider.fetch_candles(normalized, timeframe, since, until)

    # 存入 DB
    for c in candles:
        orm = CandleORM(
            symbol=c.symbol, timeframe=c.timeframe, open_time=c.open_time,
            open=c.open, high=c.high, low=c.low, close=c.close,
            volume=c.volume, source=source_name,
        )
        session.add(orm)
    session.commit()
    logger.info(f"已快取 {len(candles)} 根 K 線到 DB")

    return candles
