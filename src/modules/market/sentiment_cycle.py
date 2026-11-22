"""A-share speculative sentiment cycle: limit-up breadth -> main line -> position tier.

This is a PanWatch-native port of the public ``a-share-sentiment-cycle`` idea, with
three deliberate changes for this codebase:

* no baostock scanner — the caller feeds ``Bar`` objects from ``daily_bars`` and
  ``marketdata.zt_pool``;
* board-specific price limits (10% / 20% / 30% / ST 5%), new-listing and
  ex-rights guards;
* position tiers are capped by L1 (global risk), and L2's fry/promotion rates are
  measured inside the main line rather than the whole market.

The module is pure: no DB, no network, no clock. ``EmotionEngine.update`` is a
state machine that consumes one trading day at a time, exactly like the original
project, so it can be replayed deterministically.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional, Sequence


L1_MAX = 10.0
L2_MAX = 13.0

TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4, "T5": 5}
TIER_CAPS = {
    "T0": (0.0, 0.0),
    "T1": (0.0, 0.0),
    "T2": (0.05, 0.10),
    "T3": (0.10, 0.30),
    "T4": (0.20, 0.70),
    "T5": (0.25, 0.90),
}
TIER_NAMES = {v: k for k, v in TIER_ORDER.items()}


def normalize_symbol(raw: str) -> str:
    text = str(raw or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if len(digits) <= 6 else digits[-6:]


def infer_limit_pct(symbol: str, name: str = "") -> float:
    """Return the daily price-limit percentage for a CN stock.

    ST on ChiNext/STAR uses the 20% board rule (the registration-based boards
    kept 20% for ST); main-board ST uses 5%. BSE uses 30%.
    """
    sym = normalize_symbol(symbol)
    upper = (name or "").upper()
    if sym.startswith(("4", "8", "920")):
        return 30.0
    if sym.startswith(("300", "301", "688", "689")):
        return 20.0
    if "ST" in upper:
        return 5.0
    return 10.0


def limit_prices(preclose: float, pct: float) -> tuple[float, float]:
    up = round(preclose * (1 + pct / 100.0), 2)
    down = round(preclose * (1 - pct / 100.0), 2)
    return up, down


def _tolerance(preclose: float) -> float:
    # A-share tick is 0.01; use half a tick plus a small epsilon.
    return 0.005 + 1e-9


@dataclass
class Bar:
    symbol: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    preclose: float
    turnover: float = 0.0
    industry: Optional[str] = None
    name: str = ""


@dataclass
class Signal:
    symbol: str
    name: str
    industry: Optional[str]
    board_num: int = 0
    limit_up: bool = False
    break_up: bool = False
    limit_down: bool = False
    high_face: bool = False
    sky_floor: bool = False
    no_limit: bool = False
    suspect: bool = False
    turnover: float = 0.0
    close: float = 0.0
    preclose: float = 0.0
    limit_pct: float = 10.0


@dataclass
class Config:
    systemic_drop: float = -0.025
    systemic_penalty: float = 4.0
    drop_ratio: float = 1.2
    drop_first_penalty: float = 2.0
    drop_persistence_penalty: float = 3.0
    limit_down_soft: int = 40
    limit_down_hard: int = 80
    limit_down_soft_penalty: float = 2.0
    limit_down_hard_penalty: float = 4.0
    high_face_soft: int = 8
    high_face_hard: int = 15

    sector_min_count: int = 3
    sector_hold_ratio: float = 0.5

    leader_break_warn: int = 1
    leader_break_confirm: int = 2
    sky_floor_warn: int = 1
    sky_floor_confirm: int = 2

    ebb_l2: float = 6.0
    ebb_prev_l2: float = 7.0

    baseline_window: int = 20
    default_fry_rate: float = 0.30
    default_promote_rate: float = 0.30

    # L1 caps: (max_l1, max_tier). L1 <= 4 caps at T2; 5-7 caps at T4.
    l1_tier_caps: tuple = ((4.0, 2), (7.0, 4))
    position_tiers: tuple = (
        (11.0, 0.25, 0.90, "T5"),
        (8.0, 0.20, 0.70, "T4"),
        (5.0, 0.10, 0.30, "T3"),
        (2.0, 0.05, 0.10, "T2"),
    )


@dataclass
class State:
    last_date: str = ""
    prev_main_sector: Optional[str] = None
    prev_sector_hl: int = 0
    prev_hl_codes: list[str] = field(default_factory=list)
    prev_main_hl_codes: list[str] = field(default_factory=list)
    prev_l2: Optional[float] = None
    prev_drop_trigger: bool = False
    leader_code: Optional[str] = None
    leader_break_days: int = 0
    main_fry_seq: list[float] = field(default_factory=list)
    main_promote_seq: list[float] = field(default_factory=list)
    global_fry_seq: list[float] = field(default_factory=list)
    global_promote_seq: list[float] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "State":
        raw = raw or {}
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in fields})


@dataclass
class Snapshot:
    date: str
    l1: float
    mood: str
    systemic_risk: bool
    drop_trigger: bool
    limit_up_count: int
    limit_down_count: int
    high_face_count: int
    sky_floor_count: int
    main_sector: Optional[str]
    main_sector_hl: int
    sector_sticky: bool
    l2: Optional[float]
    l2_parts: dict
    l3: str
    leader: Optional[str]
    leader_break_days: int
    fry_rate: Optional[float]
    promote_rate: Optional[float]
    global_fry_rate: Optional[float]
    global_promote_rate: Optional[float]
    fry_baseline: Optional[float]
    promote_baseline: Optional[float]
    ebb_confirmed: bool
    position_tier: Optional[str]
    single_position: Optional[float]
    total_position: Optional[float]
    data_coverage: float = 0.0
    source: str = "local"
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _mean(seq: Sequence[float], window: int, default: float) -> float:
    tail = list(seq)[-window:]
    return sum(tail) / len(tail) if len(tail) >= window else default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def count_prior_boards(history: Sequence[Bar], pct: float) -> int:
    count = 0
    for bar in reversed(list(history)):
        if bar.preclose <= 0:
            break
        up, _ = limit_prices(bar.preclose, pct)
        if bar.close >= up - _tolerance(bar.preclose):
            count += 1
        else:
            break
    return count


def classify(
    bar: Bar,
    history: Sequence[Bar] = (),
    *,
    name: str = "",
    industry: Optional[str] = None,
    zt_board_num: int = 0,
) -> Signal:
    """Classify one daily bar using only data up to and including that bar."""
    pct = infer_limit_pct(bar.symbol, name)
    signal = Signal(
        symbol=bar.symbol,
        name=name,
        industry=industry or bar.industry,
        turnover=bar.turnover,
        close=bar.close,
        preclose=bar.preclose,
        limit_pct=pct,
    )
    bars = list(history) + [bar]
    # First five listed sessions have no daily price limit; do not count them.
    if len(bars) < 6:
        signal.no_limit = True
        return signal
    if bar.preclose <= 0:
        signal.suspect = True
        return signal
    limit_up_price, limit_down_price = limit_prices(bar.preclose, pct)
    tol = _tolerance(bar.preclose)
    change = bar.close / bar.preclose - 1
    if abs(change) > pct / 100.0 * 1.6:
        signal.suspect = True
        return signal

    signal.limit_up = bar.close >= limit_up_price - tol
    signal.limit_down = bar.close <= limit_down_price + tol
    signal.break_up = (
        not signal.limit_up
        and bar.high >= limit_up_price - tol
        and bar.close < limit_up_price - tol
    )
    if zt_board_num > 0:
        signal.limit_up = True
        signal.board_num = int(zt_board_num)
    elif signal.limit_up or signal.break_up:
        signal.board_num = count_prior_boards(history, pct) + 1

    burst = 0
    for old in list(history)[-20:]:
        if old.preclose <= 0:
            continue
        up, _ = limit_prices(old.preclose, pct)
        if old.close >= up - _tolerance(old.preclose):
            burst += 1
    signal.high_face = (
        burst >= 5
        and bar.preclose > 0
        and (bar.high - bar.close) / bar.preclose >= 0.08 - 1e-9
    )
    signal.sky_floor = (
        signal.high_face
        and bar.high >= limit_up_price - tol
        and bar.close <= limit_down_price + tol
    )
    return signal


def _mood(l1: float) -> str:
    if l1 <= 4:
        return "panic"
    if l1 <= 7:
        return "neutral"
    return "euphoric"


class EmotionEngine:
    def __init__(self, config: Optional[Config] = None, state: Optional[State] = None):
        self.cfg = config or Config()
        self.state = state or State()
        self.history: list[Snapshot] = []

    def update(
        self,
        trade_date: str,
        signals: Iterable[Signal],
        hs300_return: Optional[float] = None,
        *,
        data_coverage: float = 1.0,
        source: str = "local",
        warnings: Optional[Sequence[str]] = None,
    ) -> Snapshot:
        signals = list(signals)
        hot = [s for s in signals if (s.limit_up or s.break_up) and not s.no_limit and not s.suspect]
        sealed = {s.symbol for s in signals if s.limit_up and not s.no_limit and not s.suspect}
        limit_down = sum(1 for s in signals if s.limit_down and not s.no_limit and not s.suspect)
        high_face = sum(1 for s in signals if s.high_face and not s.no_limit and not s.suspect)
        sky_floor = sum(1 for s in signals if s.sky_floor and not s.no_limit and not s.suspect)

        warnings = list(warnings or [])
        l1, systemic, drop_trigger = self._l1(hs300_return, len(sealed), limit_down, high_face)
        sector, sector_hl, sticky = self._main_sector(hot)

        main_hl = [s for s in hot if sector and s.industry == sector]
        main_high = [s for s in main_hl if s.board_num >= 3]
        main_fry = (
            sum(1 for s in main_high if s.break_up) / len(main_high)
            if len(main_high) >= 2
            else None
        )
        prev_pool = (
            set(self.state.prev_main_hl_codes)
            if sector and sector == self.state.prev_main_sector and self.state.prev_main_hl_codes
            else set(self.state.prev_hl_codes)
        )
        main_promote = (
            len(prev_pool & sealed) / len(prev_pool) if prev_pool else 0.0
        )
        global_high = [s for s in hot if s.board_num >= 3]
        global_fry = (
            sum(1 for s in global_high if s.break_up) / len(global_high)
            if len(global_high) >= 2
            else None
        )
        global_promote = (
            len(set(self.state.prev_hl_codes) & sealed) / len(self.state.prev_hl_codes)
            if self.state.prev_hl_codes
            else 0.0
        )

        fry_baseline = _mean(self.state.main_fry_seq, self.cfg.baseline_window, self.cfg.default_fry_rate)
        promote_baseline = _mean(
            self.state.main_promote_seq, self.cfg.baseline_window, self.cfg.default_promote_rate
        )
        parts = self._l2_parts(main_hl, main_high, main_fry, fry_baseline, main_promote, promote_baseline, high_face, sky_floor)
        l2 = float(min(L2_MAX, max(0.0, sum(parts.values()))))

        l3, leader, break_days = self._l3(sector, main_hl, sealed, sky_floor)
        ebb = (
            self.state.prev_l2 is not None
            and l2 <= self.cfg.ebb_l2
            and self.state.prev_l2 <= self.cfg.ebb_prev_l2
            and l3 in ("warning", "confirm")
        )

        tier, single_pos, total_pos = self._position(l2, l1, systemic)

        snapshot = Snapshot(
            date=trade_date,
            l1=l1,
            mood=_mood(l1),
            systemic_risk=systemic,
            drop_trigger=drop_trigger,
            limit_up_count=len(sealed),
            limit_down_count=limit_down,
            high_face_count=high_face,
            sky_floor_count=sky_floor,
            main_sector=sector,
            main_sector_hl=sector_hl,
            sector_sticky=sticky,
            l2=l2,
            l2_parts=parts,
            l3=l3,
            leader=leader,
            leader_break_days=break_days,
            fry_rate=main_fry,
            promote_rate=main_promote,
            global_fry_rate=global_fry,
            global_promote_rate=global_promote,
            fry_baseline=fry_baseline,
            promote_baseline=promote_baseline,
            ebb_confirmed=ebb,
            position_tier=tier,
            single_position=single_pos,
            total_position=total_pos,
            data_coverage=round(float(data_coverage), 4),
            source=source,
            warnings=warnings,
        )

        self._advance(snapshot, sealed, hot, sector, drop_trigger, main_fry, main_promote, global_fry, global_promote)
        self.history.append(snapshot)
        return snapshot

    # ------------------------------------------------------------------ L1
    def _l1(
        self,
        hs300_return: Optional[float],
        up: int,
        down: int,
        high_face: int,
    ) -> tuple[float, bool, bool]:
        cfg = self.cfg
        score = L1_MAX
        systemic = hs300_return is not None and hs300_return <= cfg.systemic_drop
        if systemic:
            score -= cfg.systemic_penalty
        # Fix the original `up > 0` guard: a 0-up / many-down day is the most
        # risk-off tape, not a day without a ratio signal.
        drop_trigger = down >= 5 and (up == 0 or down / up > cfg.drop_ratio)
        if drop_trigger:
            # A first-day ratio spike still deserves a penalty; otherwise a
            # 77-limit-down tape can score L1=10 just because it is day one.
            score -= (
                cfg.drop_persistence_penalty
                if self.state.prev_drop_trigger
                else cfg.drop_first_penalty
            )
        if high_face >= cfg.high_face_hard:
            score -= 3
        elif high_face >= cfg.high_face_soft:
            score -= 1
        # Ratio alone can look mild when both sides are small; an absolute
        # limit-down wave is still risk-off. Calibrated for the full-market pool.
        if down >= cfg.limit_down_hard:
            score -= cfg.limit_down_hard_penalty
        elif down >= cfg.limit_down_soft:
            score -= cfg.limit_down_soft_penalty
        return float(_clip(score, 0.0, L1_MAX)), systemic, drop_trigger

    # ------------------------------------------------------------ main line
    def _main_sector(self, hot: Sequence[Signal]) -> tuple[Optional[str], int, bool]:
        counts: dict[str, int] = {}
        for signal in hot:
            if signal.industry:
                counts[signal.industry] = counts.get(signal.industry, 0) + 1
        if not counts:
            return None, 0, False
        top_name, top_n = max(counts.items(), key=lambda kv: kv[1])
        prev = self.state.prev_main_sector
        if prev:
            prev_n = counts.get(prev, 0)
            floor = max(self.cfg.sector_min_count, top_n * self.cfg.sector_hold_ratio)
            if prev_n >= floor:
                return prev, prev_n, True
        if top_n < self.cfg.sector_min_count:
            return None, top_n, False
        return top_name, top_n, False

    # ------------------------------------------------------------------ L2
    def _l2_parts(
        self,
        main_hl: Sequence[Signal],
        main_high: Sequence[Signal],
        fry_rate: Optional[float],
        fry_baseline: float,
        promote_rate: float,
        promote_baseline: float,
        high_face: int,
        sky_floor: int,
    ) -> dict:
        if not main_hl:
            # No main line means no L2 score at all. Otherwise the neutral fry
            # component + face bonus would score 5 in a dead market and hand out
            # a T3 that the original project explicitly fixed.
            return {"tier": 0, "fry": 0, "promote": 0, "face": 0}

        four_plus = sum(1 for s in main_hl if s.board_num >= 4)
        tier = 4 if four_plus >= 3 else (2 if four_plus >= 1 else 0)

        if len(main_high) < 2 or fry_rate is None:
            fry = 2
        else:
            diff = fry_rate - fry_baseline
            fry = 4 if diff < 0 else (2 if diff <= 0.15 else 0)

        dev = promote_rate - promote_baseline
        promote = 2 if dev >= 0 else (1 if dev >= -0.20 else 0)

        if high_face == 0:
            face = 3
        elif high_face <= 5:
            face = 1
        else:
            face = -1 if sky_floor > 0 else 0
        return {"tier": tier, "fry": fry, "promote": promote, "face": face}

    # ------------------------------------------------------------------ L3
    def _l3(
        self,
        sector: Optional[str],
        main_hl: Sequence[Signal],
        sealed: set[str],
        sky_floor: int,
    ) -> tuple[str, Optional[str], int]:
        if not sector:
            return "none", None, 0
        candidates = [s for s in main_hl if not s.break_up and s.limit_up]
        leader = None
        if candidates:
            candidates.sort(key=lambda s: (-s.board_num, -s.turnover))
            leader = candidates[0].symbol
        if leader is None:
            leader = self.state.leader_code
        if leader is None:
            return "none", None, 0
        break_days = self.state.leader_break_days
        if leader != self.state.leader_code and self.state.leader_code is not None:
            break_days = 0
        new_break = 0 if leader in sealed else break_days + 1
        if new_break >= self.cfg.leader_break_confirm or sky_floor >= self.cfg.sky_floor_confirm:
            signal = "confirm"
        elif new_break >= self.cfg.leader_break_warn or sky_floor >= self.cfg.sky_floor_warn:
            signal = "warning"
        else:
            signal = "none"
        return signal, leader, new_break

    # -------------------------------------------------------------- position
    def _position(self, l2: float, l1: float, systemic: bool) -> tuple[Optional[str], Optional[float], Optional[float]]:
        if systemic:
            return "T0", 0.0, 0.0
        base = 1
        for cut, single, total, name in self.cfg.position_tiers:
            if l2 >= cut:
                base = TIER_ORDER[name]
                single_cap, total_cap = single, total
                break
        else:
            single_cap, total_cap = 0.0, 0.0
        cap = 5
        for max_l1, max_tier in self.cfg.l1_tier_caps:
            if l1 <= max_l1:
                cap = max_tier
                break
        index = min(base, cap)
        if index <= 1:
            return TIER_NAMES[index], 0.0, 0.0
        tier = TIER_NAMES[index]
        if index == TIER_ORDER.get("T2", 2):
            single_cap, total_cap = TIER_CAPS["T2"]
        elif index == TIER_ORDER.get("T3", 3):
            single_cap, total_cap = TIER_CAPS["T3"]
        elif index == TIER_ORDER.get("T4", 4):
            single_cap, total_cap = TIER_CAPS["T4"]
        else:
            single_cap, total_cap = TIER_CAPS["T5"]
        return tier, single_cap, total_cap

    # ----------------------------------------------------------------- state
    def _advance(
        self,
        snapshot: Snapshot,
        sealed: set[str],
        hot: Sequence[Signal],
        sector: Optional[str],
        drop_trigger: bool,
        main_fry: Optional[float],
        main_promote: float,
        global_fry: Optional[float],
        global_promote: float,
    ) -> None:
        state = self.state
        main_sealed = sorted(
            {s.symbol for s in hot if s.industry == sector and s.limit_up}
        ) if sector else []
        state.last_date = snapshot.date
        state.prev_main_sector = snapshot.main_sector
        state.prev_sector_hl = snapshot.main_sector_hl
        state.prev_hl_codes = sorted(sealed)
        state.prev_main_hl_codes = main_sealed
        state.prev_l2 = snapshot.l2
        state.prev_drop_trigger = drop_trigger
        state.leader_code = snapshot.leader
        state.leader_break_days = snapshot.leader_break_days
        if main_fry is not None:
            state.main_fry_seq = (state.main_fry_seq + [main_fry])[-60:]
        if global_fry is not None:
            state.global_fry_seq = (state.global_fry_seq + [global_fry])[-60:]
        state.main_promote_seq = (state.main_promote_seq + [main_promote])[-60:]
        state.global_promote_seq = (state.global_promote_seq + [global_promote])[-60:]
        state.updated_at = snapshot.date


__all__ = [
    "Bar", "Signal", "Config", "State", "Snapshot", "EmotionEngine",
    "infer_limit_pct", "limit_prices", "classify", "normalize_symbol",
    "TIER_CAPS", "TIER_ORDER",
]
