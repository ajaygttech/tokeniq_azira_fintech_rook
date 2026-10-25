"""Fixed-contract linear futures simulation; same signals/settings as forward."""
from datetime import datetime, timezone
from math import ceil
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import Field
from backend.strategy import StrategyConfig, TIMEFRAMES, normalize, supertrend, protection

class Backtest(StrategyConfig):
    start: str
    end: str
    capital: float = Field(default=10000,gt=0,le=100000000)
    fee_bps: float = Field(default=5,ge=0,le=100)
    slippage_bps: float = Field(default=2,ge=0,le=100)
    data_source: Literal['testnet'] = 'testnet'
    timezone: Literal['Asia/Kolkata', 'UTC'] = 'UTC'

    def time_bounds(self, now):
        def stamp(value):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(self.timezone))
            return int(parsed.timestamp())
        start, end = stamp(self.start), stamp(self.end)
        if start >= end or end > now or ceil((end-start)/TIMEFRAMES[self.resolution]) > 5000:
            raise ValueError('Choose a past date/time range up to 5,000 candles')
        return start, end


def validate_history(candles, config, start, end):
    """Do not silently simulate missing bars or a truncated API response."""
    seconds = TIMEFRAMES[config.resolution]
    rows = [r for r in normalize(candles) if r['time'] + seconds <= end]
    selected = [r for r in rows if r['time'] >= start]
    warmup = [r for r in rows if r['time'] < start]
    if not selected:
        raise ValueError('No complete candles in this range. Move the start earlier or the end later.')
    if len(warmup) < config.atr_period:
        raise ValueError('Not enough historical candles before the start for ATR warmup')
    if selected[0]['time'] - start >= seconds or end - (selected[-1]['time'] + seconds) >= seconds:
        raise ValueError('Historical data does not cover the full requested range. Backtest was not run.')
    if any(b['time'] - a['time'] != seconds for a, b in zip(rows, rows[1:])):
        raise ValueError('Historical candles contain gaps. Choose another range; missing bars are not simulated.')
    return rows, selected, len(warmup)


def run_backtest(candles, config, contract_value=0.001, tick_size=0.1, trade_start=None, trade_end=None):
    seconds = TIMEFRAMES[config.resolution]
    rows = [row for row in candles if trade_end is None or row['time'] + seconds <= trade_end]
    points = supertrend(rows,config.atr_period,config.multiplier)
    if len(points) < config.atr_period+2:
        raise ValueError('Not enough closed candles for ATR warmup')
    cash, peak, max_dd = config.capital, config.capital, 0
    fee, slip = config.fee_bps/10000, config.slippage_bps/10000
    units = config.quantity*contract_value
    position, trades, curve = None, [], []
    daily_start, day, halted = cash, None, False
    diagnostics = {'signals':0,'entry_candidates':0,'blocked_entries':0,
                   'blocked_by_reason':{'max_position_value':0,'insufficient_capital':0,'daily_loss_limit':0},
                   'examples':[],'min_blocked_notional':None,'max_blocked_notional':None}

    def close(price,row,reason,precision='intrabar',signal_time=None):
        nonlocal cash, position
        p = position
        exit_price = price*(1-p['direction']*slip)
        exit_fee = units*exit_price*fee
        pnl = p['direction']*units*(exit_price-p['entry'])-p['entry_fee']-exit_fee
        cash += p['direction']*units*(exit_price-p['entry'])-exit_fee
        exit_time = row['time'] + seconds if precision == 'close' else row['time']
        trades.append({'time':exit_time,'exit_bar_time':row['time'],'exit_time_precision':precision,
                       'entry_time':p['time'],'entry_signal_time':p['signal_time'],'exit_signal_time':signal_time,
                       'side':'LONG' if p['direction']==1 else 'SHORT',
                       'entry':p['entry'],'exit':exit_price,'quantity':config.quantity,'units':units,
                       'pnl':round(pnl,8),'fees':p['entry_fee']+exit_fee,'reason':reason})
        position = None

    for i,row in enumerate(points):
        if trade_start is not None and row['time'] < trade_start:
            continue
        current_day = datetime.fromtimestamp(row['time'],timezone.utc).date()
        if day != current_day:
            day, daily_start, halted = current_day, cash, False
        direction = points[i-1]['signal'] if i else 0
        if direction:
            diagnostics['signals'] += 1
        if position and direction and direction != position['direction'] and config.exit_mode != 'sl_tp':
            close(row['open'],row,'flip','open',points[i-1]['time'])
        if daily_start-cash >= config.daily_loss_limit:
            halted = True
        if direction and not position:
            diagnostics['entry_candidates'] += 1
            entry = row['open']*(1+direction*slip)
            notional = entry*units
            reasons = []
            if notional > config.max_notional:
                reasons.append('max_position_value')
            if cash <= 0 or notional > cash:
                reasons.append('insufficient_capital')
            if halted:
                reasons.append('daily_loss_limit')
            if reasons:
                diagnostics['blocked_entries'] += 1
                for reason in reasons:
                    diagnostics['blocked_by_reason'][reason] += 1
                minimum, maximum = diagnostics['min_blocked_notional'], diagnostics['max_blocked_notional']
                diagnostics['min_blocked_notional'] = notional if minimum is None else min(minimum,notional)
                diagnostics['max_blocked_notional'] = notional if maximum is None else max(maximum,notional)
                if len(diagnostics['examples']) < 20:
                    diagnostics['examples'].append({'time':row['time'],'side':'LONG' if direction==1 else 'SHORT',
                        'required_notional':notional,'max_notional':config.max_notional,
                        'available_capital':cash,'reasons':reasons})
            else:
                levels = protection(entry,direction,config,tick_size)
                entry_fee = notional*fee
                cash -= entry_fee
                position = {'direction':direction,'entry':entry,'entry_fee':entry_fee,'time':row['time'],
                            'signal_time':points[i-1]['time'],**levels}
        if position:
            p, stop, target = position, position['stop'], position['target']
            stop_hit = stop is not None and (row['low']<=stop if p['direction']==1 else row['high']>=stop)
            target_hit = target is not None and (row['high']>=target if p['direction']==1 else row['low']<=target)
            if stop_hit:
                close(min(row['open'],stop) if p['direction']==1 else max(row['open'],stop),row,'stop_loss')
            elif target_hit:
                close(target,row,'target')
        equity = cash+(position['direction']*units*(row['close']-position['entry']) if position else 0)
        if position and daily_start-equity >= config.daily_loss_limit:
            close(row['close'],row,'daily_loss_limit','close')
            equity, halted = cash, True
        if i==len(points)-1 and position:
            close(row['close'],row,'end_of_test','close')
            equity = cash
        peak = max(peak,equity)
        dd = (peak-equity)/peak*100
        max_dd = max(max_dd,dd)
        curve.append({'time':row['time']+seconds,'equity':round(equity,4),'drawdown':round(dd,4)})
    wins, losses = [t['pnl'] for t in trades if t['pnl']>0],[t['pnl'] for t in trades if t['pnl']<0]
    return {'equity':curve,'trades':trades,'metrics':{'net_pnl':round(cash-config.capital,4),
            'return_pct':round((cash/config.capital-1)*100,4),'max_drawdown':round(max_dd,4),
            'total_trades':len(trades),'win_rate':round(len(wins)/len(trades)*100,2) if trades else 0,
            'profit_factor':round(sum(wins)/abs(sum(losses)),3) if losses else None,
            'fees':round(sum(t['fees'] for t in trades),6)},'candles':len(curve),
            'engine_version':2,'strategy':'Supertrend flips · long / short','diagnostics':diagnostics,
            'contract_value':contract_value,'indicator':points[-500:],
            'assumptions':'Closed-candle signal, next-open fill; fixed contracts; 1x collateral; stop-first on ambiguous bars; fees/slippage included; funding and liquidation excluded.'}
