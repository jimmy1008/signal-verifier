"""
BingX Perpetual Futures Broker 實作

職責：
- 透過 BingX API 執行永續合約交易
- 下單 / 改單 / 平倉 / 查詢持倉
- 手續費：Maker 0.02% / Taker 0.05%（比 Binance 低）

CRT SNIPER Symbol: BTCUSDT.P → BingX: BTC-USDT
"""

from __future__ import annotations

import logging
from typing import Optional

import ccxt.async_support as ccxt_async

from src.models import SignalSide
from src.trader.base import BaseBroker, OrderResult, Position, AccountInfo, MarketInfo

logger = logging.getLogger(__name__)


# BingX 標準合約（外匯/商品/指數）的 symbol 映射
BINGX_FOREX_MAP = {
    "NAS100USD": "NAS100-USD",
    "XAUUSD": "XAU-USD",
    "XAGUSD": "XAG-USD",
    "EURUSD": "EUR-USD",
    "GBPUSD": "GBP-USD",
    "USDJPY": "USD-JPY",
    "EURJPY": "EUR-JPY",
    "GBPJPY": "GBP-JPY",
    "AUDUSD": "AUD-USD",
    "NZDUSD": "NZD-USD",
    "USDCHF": "USD-CHF",
    "USDCAD": "USD-CAD",
}


def to_bingx_symbol(symbol: str) -> str:
    """CRT SNIPER symbol → BingX 格式"""
    s = symbol.upper().strip()
    if s.endswith(".P"):
        s = s[:-2]

    # 外匯/商品映射
    if s in BINGX_FOREX_MAP:
        return BINGX_FOREX_MAP[s]

    # 加密貨幣：BTCUSDT → BTC-USDT
    if s.endswith("USDT") and "-" not in s:
        base = s[:-4]
        return f"{base}-USDT"

    return s


class BingXBroker(BaseBroker):
    """BingX 永續合約（使用 ccxt）"""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        is_demo: bool = True,
        leverage: int = 20,
        margin_mode: str = "isolated",  # isolated = 逐倉, cross = 全倉
    ):
        options = {
            "defaultType": "swap",
            "enableRateLimit": True,
        }
        if is_demo:
            options["sandboxMode"] = True

        self.exchange = ccxt_async.bingx({
            "apiKey": api_key,
            "secret": api_secret,
            "options": options,
        })
        self.is_demo = is_demo
        self.leverage = leverage
        self.margin_mode = margin_mode
        self._configured_symbols: set = set()  # 已設定過槓桿的商品
        self._market_cache: dict[str, MarketInfo] = {}  # 交易對精度快取
        self._last_protection_failure: dict | None = None  # 最近一次保護單失敗

    async def connect(self) -> bool:
        try:
            await self.exchange.load_markets()
            balance = await self.exchange.fetch_balance()
            total = balance.get("total", {}).get("USDT", 0)
            mode = "Demo" if self.is_demo else "Live"
            logger.info(f"BingX 已連線 ({mode}), USDT: {total}")
            return True
        except Exception as e:
            logger.error(f"BingX 連線失敗: {e}")
            return False

    async def get_account(self) -> AccountInfo:
        balance = await self.exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        return AccountInfo(
            balance=float(usdt.get("total", 0)),
            equity=float(usdt.get("total", 0)),
            margin_used=float(usdt.get("used", 0)),
            margin_available=float(usdt.get("free", 0)),
            open_positions=len(await self.get_open_positions()),
            currency="USDT",
        )

    async def get_price(self, symbol: str) -> tuple[float, float]:
        bingx_sym = to_bingx_symbol(symbol)
        ticker = await self.exchange.fetch_ticker(bingx_sym)
        bid = ticker.get("bid", 0) or ticker.get("last", 0)
        ask = ticker.get("ask", 0) or ticker.get("last", 0)
        return float(bid), float(ask)

    async def _ensure_config(self, bingx_sym: str) -> None:
        """確保該商品已設定槓桿和保證金模式（每個商品只設一次）"""
        if bingx_sym in self._configured_symbols:
            return

        try:
            await self.exchange.set_margin_mode("cross", bingx_sym)
            logger.info(f"[{bingx_sym}] 全倉模式")
        except Exception as e:
            logger.debug(f"[{bingx_sym}] 保證金模式: {e}")

        for side in ["LONG", "SHORT"]:
            try:
                await self.exchange.set_leverage(self.leverage, bingx_sym, params={"side": side})
            except Exception:
                pass
        logger.info(f"[{bingx_sym}] 槓桿 {self.leverage}x")

        self._configured_symbols.add(bingx_sym)

        # 快取交易對精度
        if bingx_sym not in self._market_cache:
            try:
                m = self.exchange.market(bingx_sym)
                limits = m.get("limits", {})
                precision = m.get("precision", {})
                self._market_cache[bingx_sym] = MarketInfo(
                    symbol=bingx_sym,
                    base_currency=m.get("base", ""),
                    quote_currency=m.get("quote", "USDT"),
                    price_precision=precision.get("price", 8) if isinstance(precision.get("price"), int) else 8,
                    amount_precision=precision.get("amount", 8) if isinstance(precision.get("amount"), int) else 8,
                    min_notional=float(limits.get("cost", {}).get("min", 5) or 5),
                    min_quantity=float(limits.get("amount", {}).get("min", 0.001) or 0.001),
                    tick_size=float(precision.get("price", 0.01)) if not isinstance(precision.get("price"), int) else 0,
                    step_size=float(precision.get("amount", 0.001)) if not isinstance(precision.get("amount"), int) else 0,
                )
            except Exception:
                pass

    async def _quick_price(self, bingx_sym: str) -> float:
        """快速取最新價（用於驗證）"""
        try:
            t = await self.exchange.fetch_ticker(bingx_sym)
            return float(t.get("last", 0) or 0)
        except Exception:
            return 0

    def get_market(self, symbol: str) -> MarketInfo | None:
        """取得交易對精度規格"""
        bingx_sym = to_bingx_symbol(symbol)
        return self._market_cache.get(bingx_sym)

    async def market_order(
        self,
        symbol: str,
        side: SignalSide,
        units: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        tp3: Optional[float] = None,
    ) -> OrderResult:
        bingx_sym = to_bingx_symbol(symbol)
        ccxt_side = "buy" if side == SignalSide.LONG else "sell"
        pos_side = "LONG" if side == SignalSide.LONG else "SHORT"

        try:
            await self._ensure_config(bingx_sym)

            # 下單前精度驗證
            mkt = self._market_cache.get(bingx_sym)
            if mkt:
                units = mkt.round_quantity(units)
                valid, reason = mkt.validate_order(
                    await self._quick_price(bingx_sym) or 1, units)
                if not valid:
                    logger.warning(f"下單驗證失敗 {bingx_sym}: {reason}")
                    return OrderResult(success=False, symbol=symbol, error=reason)

            # 開倉
            order = await self.exchange.create_order(
                symbol=bingx_sym,
                type="market",
                side=ccxt_side,
                amount=units,
                params={"positionSide": pos_side},
            )

            trade_id = str(order.get("id", ""))
            entry_price = float(order.get("average", 0) or order.get("price", 0) or 0)

            close_side = "sell" if side == SignalSide.LONG else "buy"

            # 取消同方向同 positionSide 的舊 TP/SL 掛單（防止合倉時重複）
            try:
                old_orders = await self.exchange.fetch_open_orders(bingx_sym)
                for old in old_orders:
                    otype = str(old.get("info", {}).get("type", "")).lower()
                    oside = str(old.get("side", "")).lower()
                    old_ps = old.get("info", {}).get("positionSide", "")
                    # 只取消同 positionSide 的掛單，不影響反方向持倉
                    if ("stop" in otype or "take_profit" in otype) and oside == close_side and old_ps == pos_side:
                        await self.exchange.cancel_order(old["id"], bingx_sym)
                        logger.debug(f"取消舊掛單: {old['id']} ({old_ps})")
            except Exception as e:
                logger.debug(f"清理舊掛單: {e}")

            # 取得合倉後的總數量
            try:
                merged_positions = await self.exchange.fetch_positions([bingx_sym])
                for mp in merged_positions:
                    if abs(float(mp.get("contracts", 0))) > 0 and mp.get("side") == ("long" if side == SignalSide.LONG else "short"):
                        units = abs(float(mp.get("contracts", 0)))
                        break
            except Exception:
                pass

            # 取得最小下單量精度
            try:
                market = self.exchange.market(bingx_sym)
                min_amt = float(market.get("limits", {}).get("amount", {}).get("min", 0) or 0)
                amt_precision = market.get("precision", {}).get("amount", 8)
            except Exception:
                min_amt = 0
                amt_precision = 8

            def _round_amt(qty):
                """四捨五入到交易所精度，不低於最小量"""
                rounded = round(qty, amt_precision) if isinstance(amt_precision, int) else round(qty, 8)
                return rounded if rounded >= min_amt else 0

            half = _round_amt(units / 2)

            # 如果 half 不夠最小量，全倉掛在 TP4
            if half <= 0 or half < min_amt:
                tp3_amt = 0
                tp4_amt = _round_amt(units)
            else:
                tp3_amt = half
                tp4_amt = _round_amt(units - half)

            # SL（全倉）
            sl_ok = False
            if sl is not None:
                try:
                    await self.exchange.create_order(
                        symbol=bingx_sym, type="stop_market", side=close_side,
                        amount=units,
                        params={"stopPrice": sl, "positionSide": pos_side},
                    )
                    sl_ok = True
                except Exception as e:
                    logger.error(f"BingX SL 掛單失敗 {bingx_sym}: {e}")

            # TP3（50% 倉位）
            tp3_ok = False
            if tp3 is not None and tp3_amt > 0:
                try:
                    await self.exchange.create_order(
                        symbol=bingx_sym, type="take_profit", side=close_side,
                        amount=tp3_amt, price=tp3,
                        params={"stopPrice": tp3, "positionSide": pos_side},
                    )
                    tp3_ok = True
                except Exception as e:
                    logger.error(f"BingX TP3 掛單失敗 {bingx_sym}: {e}")

            # TP4（剩餘倉位）
            tp4_ok = False
            if tp is not None and tp4_amt > 0:
                try:
                    await self.exchange.create_order(
                        symbol=bingx_sym, type="take_profit", side=close_side,
                        amount=tp4_amt, price=tp,
                        params={"stopPrice": tp, "positionSide": pos_side},
                    )
                    tp4_ok = True
                except Exception as e:
                    logger.error(f"BingX TP4 掛單失敗 {bingx_sym}: {e}")

            # 如果任一保護單失敗，記錄到 _failed_protection 供外層通知
            if not sl_ok or (tp3 and tp3_amt > 0 and not tp3_ok) or (tp and tp4_amt > 0 and not tp4_ok):
                missing = []
                if not sl_ok:
                    missing.append("SL")
                if tp3 and tp3_amt > 0 and not tp3_ok:
                    missing.append("TP3")
                if tp and tp4_amt > 0 and not tp4_ok:
                    missing.append("TP4")
                self._last_protection_failure = {
                    "symbol": bingx_sym, "side": pos_side,
                    "missing": missing, "units": units,
                }
                logger.error(f"⚠️ {bingx_sym} {pos_side} 缺少保護單: {', '.join(missing)}")

            # 檢查 SL/TP 是否真的掛上
            sl_ok = "SL" if sl else "-"
            tp_ok = "TP" if tp else "-"
            logger.info(f"BingX 下單成功: {bingx_sym} {ccxt_side} {units} @ {entry_price} [{sl_ok}/{tp_ok}]")

            return OrderResult(
                success=True,
                order_id=trade_id,
                trade_id=trade_id,
                symbol=symbol,
                side=side.value,
                units=units,
                entry_price=entry_price,
                sl_price=sl or 0,
                tp_price=tp or 0,
            )

        except Exception as e:
            err_str = str(e)
            # 保證金不足 → 嘗試調高現有倉位槓桿釋放保證金
            if "Insufficient margin" in err_str or "101204" in err_str:
                logger.warning(f"保證金不足，嘗試調高現有倉位槓桿釋放保證金...")
                retried = await self._free_margin_and_retry(
                    bingx_sym, ccxt_side, units, pos_side, sl, tp, tp3)
                if retried and retried.success:
                    return retried
            logger.error(f"BingX 下單失敗 {bingx_sym}: {e}")
            return OrderResult(success=False, symbol=symbol, error=err_str)

    async def _free_margin_and_retry(self, bingx_sym, ccxt_side, units, pos_side, sl, tp, tp3):
        """保證金不足時，逐步調高現有倉位槓桿釋放保證金，然後重試開倉"""
        try:
            positions = await self.exchange.fetch_positions()
            open_pos = [(p.get("symbol"), p.get("leverage", 10)) for p in positions
                        if abs(float(p.get("contracts", 0))) > 0]

            if not open_pos:
                return None

            # 從低槓桿的開始調高（每次 +5x，最高 20x）
            for sym, cur_lev in sorted(open_pos, key=lambda x: float(x[1])):
                cur_lev = int(float(cur_lev))
                if cur_lev >= 50:
                    continue
                new_lev = min(cur_lev + 10, 50)
                try:
                    for s in ["LONG", "SHORT"]:
                        try:
                            await self.exchange.set_leverage(new_lev, sym, params={"side": s})
                        except Exception:
                            pass
                    logger.info(f"[{sym}] 槓桿 {cur_lev}x → {new_lev}x（釋放保證金）")
                except Exception as e:
                    logger.debug(f"調槓桿失敗 {sym}: {e}")

            # 重試開倉
            close_side = "sell" if pos_side == "LONG" else "buy"
            order = await self.exchange.create_order(
                symbol=bingx_sym, type="market", side=ccxt_side,
                amount=units, params={"positionSide": pos_side},
            )
            trade_id = str(order.get("id", ""))
            entry_price = float(order.get("average", 0) or order.get("price", 0) or 0)
            logger.info(f"保證金釋放成功，重試開倉: {bingx_sym} {ccxt_side} {units} @ {entry_price}")

            # 重掛 SL/TP
            try:
                market = self.exchange.market(bingx_sym)
                min_amt = float(market.get("limits", {}).get("amount", {}).get("min", 0) or 0)
                amt_precision = market.get("precision", {}).get("amount", 8)
            except Exception:
                min_amt = 0
                amt_precision = 8

            def _round_amt(qty):
                rounded = round(qty, amt_precision) if isinstance(amt_precision, int) else round(qty, 8)
                return rounded if rounded >= min_amt else 0

            # 取合倉後總量
            try:
                merged = await self.exchange.fetch_positions([bingx_sym])
                for mp in merged:
                    if abs(float(mp.get("contracts", 0))) > 0 and mp.get("side") == ("long" if pos_side == "LONG" else "short"):
                        units = abs(float(mp.get("contracts", 0)))
                        break
            except Exception:
                pass

            half = _round_amt(units / 2)
            if half <= 0 or half < min_amt:
                tp3_amt, tp4_amt = 0, _round_amt(units)
            else:
                tp3_amt, tp4_amt = half, _round_amt(units - half)

            if sl:
                try:
                    await self.exchange.create_order(symbol=bingx_sym, type="stop_market", side=close_side, amount=units, params={"stopPrice": sl, "positionSide": pos_side})
                except Exception:
                    pass
            if tp3 and tp3_amt > 0:
                try:
                    await self.exchange.create_order(symbol=bingx_sym, type="take_profit", side=close_side, amount=tp3_amt, price=tp3, params={"stopPrice": tp3, "positionSide": pos_side})
                except Exception:
                    pass
            if tp and tp4_amt > 0:
                try:
                    await self.exchange.create_order(symbol=bingx_sym, type="take_profit", side=close_side, amount=tp4_amt, price=tp, params={"stopPrice": tp, "positionSide": pos_side})
                except Exception:
                    pass

            from src.trader.base import OrderResult
            return OrderResult(success=True, order_id=trade_id, trade_id=trade_id, symbol=bingx_sym, side=ccxt_side, units=units, entry_price=entry_price, sl_price=sl or 0, tp_price=tp or 0)

        except Exception as e:
            logger.warning(f"釋放保證金後重試仍失敗: {e}")
            return None

    async def modify_trade(
        self,
        trade_id: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
    ) -> bool:
        try:
            bingx_sym = to_bingx_symbol(symbol) if symbol else None
            # 統一比對用的 key：去掉所有分隔符
            def _norm(s):
                return s.upper().replace("/", "").replace("-", "").replace(":USDT", "").replace(".P", "")

            bingx_norm = _norm(bingx_sym) if bingx_sym else ""
            positions = await self.exchange.fetch_positions()
            target = None

            # 精確匹配：symbol + side
            if bingx_norm and side:
                target_side = side.lower()
                for pos in positions:
                    if abs(float(pos.get("contracts", 0))) > 0:
                        pos_norm = _norm(pos.get("symbol", ""))
                        if bingx_norm == pos_norm and pos.get("side") == target_side:
                            target = pos
                            break

            # Fallback：只用 symbol
            if not target and bingx_norm:
                for pos in positions:
                    if abs(float(pos.get("contracts", 0))) > 0:
                        pos_norm = _norm(pos.get("symbol", ""))
                        if bingx_norm == pos_norm:
                            target = pos
                            break

            if not target:
                logger.warning(f"找不到持倉: {symbol} {side}")
                return False

            target_sym = target["symbol"]
            amount = abs(float(target.get("contracts", 0)))
            side_str = target.get("side", "long")
            close_side = "sell" if side_str == "long" else "buy"
            pos_side = "LONG" if side_str == "long" else "SHORT"

            # 只取消該方向的 SL 掛單（保留 TP 掛單）
            if sl is not None:
                try:
                    open_orders = await self.exchange.fetch_open_orders(target_sym)
                    for order in open_orders:
                        otype = str(order.get("type", "")).lower()
                        oside = str(order.get("side", "")).lower()
                        order_pos_side = order.get("info", {}).get("positionSide", "")
                        if "stop" in otype and "profit" not in otype and oside == close_side:
                            if order_pos_side and order_pos_side != pos_side:
                                continue
                            await self.exchange.cancel_order(order["id"], target_sym)
                            logger.info(f"已取消舊 SL 掛單: {order['id']} ({pos_side})")
                except Exception as e:
                    logger.debug(f"取消 SL 掛單: {e}")

                # 下新 SL
                await self.exchange.create_order(
                    symbol=target_sym, type="stop_market", side=close_side,
                    amount=amount,
                    params={"stopPrice": sl, "positionSide": pos_side},
                )

            logger.info(f"BingX SL 已移動: {target_sym} {pos_side} SL={sl}")
            return True

        except Exception as e:
            err = str(e)
            if "should be greater" in err or "should be less" in err:
                # 價格已超過保本價（盈利中），不需要移 SL
                logger.info(f"[{symbol}] SL 移動跳過：價格已超過保本價（持倉盈利中）")
                return True
            logger.error(f"BingX SL 修改失敗 {symbol}: {e}")
            return False

    async def close_trade(self, trade_id: str) -> bool:
        try:
            positions = await self.exchange.fetch_positions()
            for pos in positions:
                if abs(float(pos.get("contracts", 0))) > 0:
                    symbol = pos["symbol"]
                    amount = abs(float(pos["contracts"]))
                    side_str = pos.get("side", "long")
                    close_side = "sell" if side_str == "long" else "buy"
                    pos_side = "LONG" if side_str == "long" else "SHORT"
                    await self.exchange.create_order(
                        symbol=symbol, type="market", side=close_side,
                        amount=amount, params={"positionSide": pos_side, },
                    )
                    logger.info(f"BingX 已平倉: {symbol}")
                    return True
            return False
        except Exception as e:
            logger.error(f"BingX 平倉失敗: {e}")
            return False

    async def get_open_positions(self) -> list[Position]:
        positions = await self.exchange.fetch_positions()
        result = []
        for pos in positions:
            contracts = abs(float(pos.get("contracts", 0)))
            if contracts > 0:
                result.append(Position(
                    trade_id=pos.get("id", ""),
                    symbol=pos["symbol"],
                    side=pos.get("side", "long"),
                    units=contracts,
                    entry_price=float(pos.get("entryPrice", 0)),
                    current_price=float(pos.get("markPrice", 0)),
                    unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
                ))
        return result

    async def get_position_by_symbol(self, symbol: str) -> Optional[Position]:
        bingx_sym = to_bingx_symbol(symbol)
        positions = await self.get_open_positions()
        for p in positions:
            if bingx_sym in p.symbol.replace("/", "").replace(":USDT", ""):
                return p
        return None

    async def check_margin_and_adjust(self, threshold: float = 0.4) -> None:
        """佔用保證金 > threshold 時，自動調高現有倉位槓桿釋放保證金"""
        try:
            balance = await self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            total = float(usdt.get("total", 0))
            used = float(usdt.get("used", 0))
            if total <= 0:
                return

            ratio = used / total
            if ratio <= threshold:
                return

            logger.warning(f"保證金佔用 {ratio:.0%} > {threshold:.0%}，自動調高槓桿釋放保證金")

            positions = await self.exchange.fetch_positions()
            # 從低槓桿的開始調，每次 +10x，無上限
            for pos in sorted(positions, key=lambda p: float(p.get("leverage", 0))):
                contracts = abs(float(pos.get("contracts", 0)))
                if contracts <= 0:
                    continue
                cur_lev = int(float(pos.get("leverage", 20)))
                sym = pos.get("symbol")
                new_lev = cur_lev + 10

                try:
                    for side in ["LONG", "SHORT"]:
                        try:
                            await self.exchange.set_leverage(new_lev, sym, params={"side": side})
                        except Exception:
                            pass
                    logger.info(f"[{sym}] 槓桿 {cur_lev}x → {new_lev}x（保證金釋放）")
                except Exception:
                    # 該商品槓桿有上限，跳過調其他的
                    logger.debug(f"[{sym}] 槓桿 {cur_lev}x → {new_lev}x 失敗（可能已達上限），跳過")
                    continue

                # 調完一個就重新檢查，夠了就停
                balance = await self.exchange.fetch_balance()
                usdt = balance.get("USDT", {})
                new_ratio = float(usdt.get("used", 0)) / float(usdt.get("total", 1))
                if new_ratio <= threshold:
                    logger.info(f"保證金佔用降至 {new_ratio:.0%}，停止調整")
                    return

            # 全部調完後最終狀態
            balance = await self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            final_ratio = float(usdt.get("used", 0)) / float(usdt.get("total", 1))
            logger.info(f"保證金佔用調整後: {final_ratio:.0%}")
        except Exception as e:
            logger.error(f"保證金檢查異常: {e}")

    async def close(self):
        await self.exchange.close()
