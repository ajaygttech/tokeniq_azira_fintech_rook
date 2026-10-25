"""Closed-candle Supertrend shared by forward execution and backtests."""
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

TIMEFRAMES = {'1m':60,'3m':180,'5m':300,'15m':900,'30m':1800,'1h':3600,
              '2h':7200,'4h':14400,'6h':21600,'1d':86400,'1w':604800}

class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    symbol: str = Field(default='BTCUSD', pattern=r'^[A-Z0-9]{3,24}$')
    resolution: str = '5m'
    atr_period: int = Field(default=10, ge=2, le=200)
    multiplier: float = Field(default=3, gt=0, le=30)
    quantity: int = Field(default=1, ge=1, le=100000)
    exit_mode: Literal['opposite_signal', 'sl_tp', 'both'] = 'both'
    sl_points: float = Field(default=100, ge=0, le=1000000)
    target_points: float = Field(default=200, ge=0, le=1000000)
    max_notional: float = Field(default=1000, gt=0, le=1000000)
    daily_loss_limit: float = Field(default=100, gt=0, le=100000)

    @model_validator(mode='after')
    def valid_timeframe(self):
        if self.resolution not in TIMEFRAMES:
            raise ValueError('Unsupported exchange timeframe')
        if self.exit_mode == 'sl_tp' and not (self.sl_points or self.target_points):
            raise ValueError('SL/TP mode requires a positive stop loss or target')
        return self


def normalize(candles):
    rows = {}
    for raw in candles:
        row = {'time':int(raw['time']), **{k:float(raw[k]) for k in ('open','high','low','close')}}
        if not all(math.isfinite(row[k]) and row[k] > 0 for k in ('open','high','low','close')):
            raise ValueError('Candle contains invalid prices')
        if row['low'] > min(row['open'],row['close']) or row['high'] < max(row['open'],row['close']) or row['low'] > row['high']:
            raise ValueError('Invalid OHLC candle')
        rows[row['time']] = row
    return sorted(rows.values(),key=lambda r:r['time'])


def supertrend(candles, period=10, multiplier=3):
    """Wilder RMA ATR; previous-close final bands. Initial red seed is not a flip."""
    result, trs = [], []
    atr = upper = lower = previous_close = None
    direction = -1
    for row in normalize(candles):
        high, low, close = row['high'], row['low'], row['close']
        tr = high-low if previous_close is None else max(high-low,abs(high-previous_close),abs(low-previous_close))
        trs.append(tr)
        point = {**row,'atr':None,'supertrend':None,'direction':0,'signal':0}
        if len(trs) >= period:
            atr = sum(trs[-period:])/period if atr is None else (atr*(period-1)+tr)/period
            basic_upper, basic_lower = (high+low)/2+multiplier*atr, (high+low)/2-multiplier*atr
            if upper is None:
                upper, lower = basic_upper, basic_lower
            else:
                old_upper, old_lower, old_direction = upper, lower, direction
                upper = basic_upper if basic_upper < old_upper or previous_close > old_upper else old_upper
                lower = basic_lower if basic_lower > old_lower or previous_close < old_lower else old_lower
                direction = (1 if close > upper else -1) if old_direction == -1 else (-1 if close < lower else 1)
                if direction != old_direction:
                    point['signal'] = direction
            point.update(atr=atr,supertrend=lower if direction==1 else upper,direction=direction)
        result.append(point)
        previous_close = close
    return result


def protection(entry, direction, config, tick=0):
    """Points are quote-price units, not ticks, percent or currency P&L."""
    if config.exit_mode == 'opposite_signal':
        return {'stop':None, 'target':None}
    def rounded(value):
        if value <= 0:
            raise ValueError('SL/target must leave a positive order price')
        return round(round(value/tick)*tick,10) if tick else value
    return {'stop':rounded(entry-direction*config.sl_points) if config.sl_points else None,
            'target':rounded(entry+direction*config.target_points) if config.target_points else None}
