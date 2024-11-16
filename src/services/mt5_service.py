import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict

import MetaTrader5 as mt5

from src.config.mt5_symbol_config import SymbolMapper
from src.utils.database_handler import DatabaseHandler

logger = logging.getLogger('MT5Service')

class MT5Service:
    def __init__(self, account: int, password: str, server: str,db_handler: DatabaseHandler = None):
        self.account = account
        self.password = password
        self.server = server
        self.initialized = False
        self.last_init_time = 0
        self.init_cooldown = 1  
        self.loop = None
        self.symbol_mapper = SymbolMapper()
        self.running = True
        self.db = db_handler
    
    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the event loop for this service."""
        self.loop = loop

    def _init(self):
                """Internal method for MT5 initialization."""
                try:
                    # Handle cooldown period
                    current_time = time.time()
                    if current_time - self.last_init_time < self.init_cooldown:
                        time.sleep(self.init_cooldown)
                    
                    # Check if already initialized
                    if self.initialized and mt5.account_info() is not None:
                        return True
                    
                    # Initialize MT5 connection
                    self.initialized = False
                    if not mt5.initialize():
                        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
                        return False
                    
                    # Login to MT5
                    if not mt5.login(self.account, password=self.password, server=self.server):
                        logger.error(f"MT5 login failed: {mt5.last_error()}")
                        mt5.shutdown()
                        return False
                    
                    # Verify account info
                    account_info = mt5.account_info()
                    if not account_info:
                        logger.error("Could not get account info")
                        mt5.shutdown()
                        return False
                    
                    self.initialized = True
                    self.last_init_time = current_time
                    print(f"✅ MT5 Connected: {account_info.login} ({account_info.server})")
                    return True
                    
                except Exception as e:
                    logger.error(f"Error initializing MT5: {e}")
                    self.initialized = False
                    return False

                # return await self.loop.run_in_executor(None, _init)

    async def _retry_operation(self, operation: Callable, max_retries: int = 3) -> Any:
        """Retry an operation with exponential backoff."""
        for attempt in range(max_retries):
            try:
                return await operation()
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Operation failed after {max_retries} attempts: {e}")
                    raise
                wait_time = (2 ** attempt) * 0.1  # 0.1s, 0.2s, 0.4s
                logger.warning(f"Operation failed, attempt {attempt + 1}/{max_retries}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)

    async def async_initialize(self) -> bool:
        """Initialize MT5 connection with cooldown asynchronously."""
        if not self.loop:
            self.loop = asyncio.get_event_loop()
        return await self._retry_operation(lambda: self.loop.run_in_executor(None, self._init))
     
    def map_symbol(self, tv_symbol: str) -> str:
        """Map TradingView symbol to MT5 symbol."""
        return self.symbol_mapper.map_symbol(tv_symbol)

    def _execute_order(self, trade_data: Dict[str, Any]) -> Dict[str, Any]:
            try:
                # Initialize MT5
                if not self.initialized and not mt5.initialize():
                    return {"error": "MT5 initialization failed"}

                # Extract trade details
                execution_data = trade_data.get('execution_data', {})
                instrument = trade_data.get('instrument') or execution_data.get('instrument')
                side = trade_data.get('side') or execution_data.get('side')
                quantity = float(trade_data.get('qty') or execution_data.get('qty', 0))
                take_profit = trade_data.get('take_profit')
                stop_loss = trade_data.get('stop_loss')
                
                # Validate required fields
                if not all([instrument, side, quantity]):
                    return {"error": "Missing required fields"}
                    
                # Map symbol and enable for trading
                mt5_symbol = self.map_symbol(instrument)
                if not mt5.symbol_select(mt5_symbol, True):
                    return {"error": f"Failed to select symbol {mt5_symbol}"}
                
                # Get symbol info
                symbol_info = mt5.symbol_info(mt5_symbol)
                if not symbol_info:
                    return {"error": f"Failed to get symbol info for {mt5_symbol}"}
                
                # Prepare order parameters
                is_buy = side.lower() == 'buy'
                order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
                price = symbol_info.ask if is_buy else symbol_info.bid
                position_id = trade_data.get('execution_data', {}).get('positionId', 'unknown')

                # Construct order request
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": mt5_symbol,
                    "volume": quantity,
                    "type": order_type,
                    "price": price,
                    "deviation": 20,
                    "magic": 234000,
                    "comment": f"TV#{position_id}",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                
                # Add TP/SL if provided
                if take_profit is not None:
                    request["tp"] = float(take_profit)
                if stop_loss is not None:
                    request["sl"] = float(stop_loss)
                
                # Send order
                result = mt5.order_send(request)
                if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                    error_msg = mt5.last_error() if not result else result.comment
                    return {
                        "error": f"Order failed: {error_msg}",
                        "retcode": result.retcode if result else None
                    }
                
                # Prepare success response
                response = {
                    "mt5_ticket": str(result.order),
                    "mt5_position": str(result.order),
                    "volume": result.volume,
                    "price": result.price,
                    "symbol": mt5_symbol,
                    "side": side,
                    "take_profit": take_profit,
                    "stop_loss": stop_loss,
                    "comment": result.comment,
                    "timestamp": datetime.now().isoformat()
                }
                
                return response
                
            except Exception as e:
                logger.error(f"Error executing order: {e}")
                return {"error": str(e)}

            # return await self.loop.run_in_executor(None, _execute)

    async def async_execute_market_order(self, trade_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute market order on MT5 asynchronously."""
        if not self.loop:
            self.loop = asyncio.get_event_loop()
        return await self._retry_operation(lambda: self.loop.run_in_executor(None, lambda: self._execute_order(trade_data)))    
        
    def _close_position(self, trade_data: Dict[str, Any]) -> Dict[str, Any]:
        """Synchronous internal method for position closing."""
        try:
            # Initialize MT5
            if not self.initialized and not mt5.initialize():
                return {"error": "MT5 initialization failed"}

            # Extract and validate required fields
            mt5_ticket = trade_data.get('mt5_ticket')
            if not mt5_ticket:
                return {"error": "MT5 ticket not provided"}

            execution_data = trade_data.get('execution_data', {})
            instrument = trade_data.get('instrument') or execution_data.get('instrument')
            close_volume = float(trade_data.get('qty') or execution_data.get('qty', 0))

            if not instrument or not close_volume:
                return {"error": "Missing required fields"}

            # Get position details
            mt5_symbol = self.map_symbol(instrument)
                        
            # Select symbol first
            if not mt5.symbol_select(mt5_symbol, True):
                return {"error": f"Failed to select symbol {mt5_symbol}"}
                
            # Get symbol info
            symbol_info = mt5.symbol_info(mt5_symbol)
            if not symbol_info:
                return {"error": f"Failed to get symbol info for {mt5_symbol}"}

            # Get position details
            positions = mt5.positions_get(ticket=int(mt5_ticket))
            if not positions:
                return {"error": f"Position #{mt5_ticket} not found"}

            position = positions[0]
            
            # Validate close volume
            if close_volume > position.volume:
                return {'error': f'Close amount {close_volume} exceeds position size {position.volume}'}
            
            is_partial = close_volume < position.volume
                
            # Determine order type based on position type
            if position.type == mt5.POSITION_TYPE_BUY:
                order_type = mt5.ORDER_TYPE_SELL
                price = symbol_info.bid
            else:
                order_type = mt5.ORDER_TYPE_BUY
                price = symbol_info.ask

            position_id = trade_data.get('execution_data', {}).get('positionId', 'unknown')

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": mt5_symbol,
                "volume": close_volume,
                "type": order_type,  # Use the correct order type
                "position": int(mt5_ticket),
                "price": price,
                "deviation": 20,
                "magic": 234000,
                "comment":  f"TV#{position_id}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            
            # Send close order
            result = mt5.order_send(request)
            
            if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                error_msg = mt5.last_error() if not result else result.comment
                return {
                    "error": f"Close failed: {error_msg}",
                    "retcode": result.retcode if result else None
                }

            # Get updated position volume after the close
            updated_positions = mt5.positions_get(ticket=int(mt5_ticket))
            remaining_volume = updated_positions[0].volume if updated_positions else 0.0

            # Prepare success response
            response = {
                "mt5_ticket": str(result.order),
                "volume": close_volume,
                "remaining_volume": remaining_volume,
                "price": result.price,
                "symbol": mt5_symbol,
                "side": "buy" if position.type == mt5.POSITION_TYPE_SELL else "sell",
                "comment": result.comment,
                "closed_position": str(position.ticket),
                "is_partial": is_partial,
                "timestamp": datetime.now().isoformat()
            }

            return response

        except Exception as e:
            logger.error(f"Error closing position: {e}")
            return {"error": str(e)}
        
    async def async_close_position(self, trade_data: Dict[str, Any]) -> Dict[str, Any]:
        """Close an existing position asynchronously."""
        if not self.loop:
            self.loop = asyncio.get_event_loop()
            
        return await self._retry_operation(
            lambda: self.loop.run_in_executor(None, self._close_position, trade_data)
        )

    def _update_position(self, trade_data: Dict[str, Any]) -> Dict[str, Any]:
        """Synchronous method to update position TP/SL in MT5."""
        try:
            # Initialize MT5
            if not self.initialized and not mt5.initialize():
                return {"error": "MT5 initialization failed"}
                
            symbol = trade_data['instrument']
            ticket = int(trade_data['mt5_ticket'])
            
            # Map symbol and enable for trading
            mt5_symbol = self.map_symbol(symbol)
            if not mt5.symbol_select(mt5_symbol, True):
                return {"error": f"Failed to select symbol {mt5_symbol}"}
            
            # Get current position
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                return {'error': f'Position #{ticket} not found'}
            
            position = positions[0]
            
            # Verify symbol match
            if position.symbol != mt5_symbol:
                return {'error': f'Position #{ticket} exists but symbol mismatch: expected {mt5_symbol}, found {position.symbol}'}

            # Get symbol info for proper formatting
            symbol_info = mt5.symbol_info(mt5_symbol)
            if not symbol_info:
                return {'error': f'Could not get symbol info for {mt5_symbol}'}
            
            digits = symbol_info.digits
            point = symbol_info.point

            # Handle None values and proper formatting
            take_profit = float(trade_data['take_profit']) if trade_data.get('take_profit') is not None else None
            stop_loss = float(trade_data['stop_loss']) if trade_data.get('stop_loss') is not None else None
            trailing_pips = float(trade_data.get('trailing_stop_pips', 0)) if trade_data.get('trailing_stop_pips') is not None else None

            # Calculate stop loss based on trailing pips if provided
            if trailing_pips and trailing_pips > 0:
                trailing_points = trailing_pips * 10  # Convert pips to points
                price_tick = mt5.symbol_info_tick(mt5_symbol)
                
                if position.type == mt5.POSITION_TYPE_BUY:
                    stop_loss = round(price_tick.bid - trailing_points * point, digits)
                else:
                    stop_loss = round(price_tick.ask + trailing_points * point, digits)

            # Validate stop loss level
            price_tick = mt5.symbol_info_tick(mt5_symbol)
            if stop_loss is not None:
                if position.type == mt5.POSITION_TYPE_BUY and stop_loss >= price_tick.bid:
                    return {'error': 'Stop Loss must be below current price for buy positions'}
                elif position.type == mt5.POSITION_TYPE_SELL and stop_loss <= price_tick.ask:
                    return {'error': 'Stop Loss must be above current price for sell positions'}

            # Round values to proper decimal places
            if take_profit is not None:
                take_profit = round(take_profit, digits)
            if stop_loss is not None:
                stop_loss = round(stop_loss, digits)

            # Prepare request
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": mt5_symbol,
                "position": ticket,
                "tp": take_profit if take_profit is not None else position.tp,
                "sl": stop_loss if stop_loss is not None else position.sl,
                "type_time": mt5.ORDER_TIME_GTC
            }

            print(f"\n📊 MT5 Modify Request:")
            print(f"Action: {request['action']}")
            print(f"Symbol: {mt5_symbol} (digits: {digits}, point: {point})")
            if stop_loss is not None:
                print(f"New SL: {stop_loss}")
            if trailing_pips:
                print(f"Based on trailing: {trailing_pips} pips ({trailing_points} points)")
            if take_profit is not None:
                print(f"New TP: {take_profit}")

            # Send the update request
            result = mt5.order_send(request)
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return {
                    'error': self._get_position_error_message(result, request),
                    'retcode': result.retcode
                }
                
            response = {
                'ticket': ticket,
                'symbol': mt5_symbol,
                'take_profit': take_profit,
                'stop_loss': stop_loss,
                'trailing_stop_pips': trailing_pips if trailing_pips else None
            }
                
            return response
            
        except Exception as e:
            logger.error(f"Error updating position: {e}")
            return {'error': str(e)}     
    
    async def monitor_trailing_stops(self):
        """Monitor and update trailing stops using EA concepts."""
        if not self.loop:
            self.loop = asyncio.get_event_loop()

        print("📊 Starting trailing stop monitor...")
        
        while self.running:
            try:
                if not self.initialized:
                    await asyncio.sleep(1)
                    continue

                positions = await self.loop.run_in_executor(None, mt5.positions_get)
                if not positions:
                    await asyncio.sleep(1)
                    continue

                for position in positions:
                    try:
                        trade = await self.db.async_get_trade_by_mt5_ticket(str(position.ticket))
                        if not trade or 'trailing_stop_pips' not in trade:
                            continue

                        trailing_pips = float(trade['trailing_stop_pips'])
                        if trailing_pips <= 0:
                            continue

                        symbol_info = mt5.symbol_info(position.symbol)
                        current_prices = mt5.symbol_info_tick(position.symbol)
                        if not symbol_info or not current_prices:
                            continue

                        # Calculate trailing distance based on instrument type
                        is_btc = 'BTC' in position.symbol
                        if is_btc:
                            # For BTCUSD, use a smaller multiplier
                            trailing_distance = trailing_pips * 0.1  # Divide by 10 for reasonable distance
                        else:
                            trailing_distance = trailing_pips * 0.0001  # For forex pairs

                        min_profit = 10  # Hardcoded for testing

                        # print(f"\n🔍 Position #{position.ticket} Status:")
                        # print(f"Symbol: {position.symbol}")
                        # print(f"Current Bid/Ask: {current_prices.bid}/{current_prices.ask}")
                        # print(f"Current SL: {position.sl}")
                        # print(f"Open Price: {position.price_open}")
                        # print(f"Trailing Distance: {trailing_distance}")

                        if position.type == mt5.POSITION_TYPE_BUY:
                            profit = current_prices.bid - position.price_open
                            # print(f"Current Profit: {profit}")
                            # print(f"Minimum Profit Required: {min_profit}")

                            if profit > min_profit:
                                new_sl = round(current_prices.bid - trailing_distance, symbol_info.digits)
                                # print(f"Calculated new SL: {new_sl}")
                                # print(f"Current SL: {position.sl}")
                                # print(f"Would update: {new_sl > position.sl}")

                                if new_sl > position.sl:
                                    print(f"📈 Moving BUY SL: {position.sl} → {new_sl} (Profit: {profit})")
                                    
                                    # Create request dictionary without None values
                                    request = {
                                        "action": mt5.TRADE_ACTION_SLTP,
                                        "position": position.ticket,
                                        "symbol": position.symbol,
                                        "type_time": mt5.ORDER_TIME_GTC
                                    }

                                    # Only add SL/TP if they exist
                                    if new_sl:
                                        request["sl"] = new_sl
                                    if position.tp:
                                        request["tp"] = position.tp

                                    print(f"Sending request: {request}")

                                    def _send_order():
                                        try:
                                            return mt5.order_send(request)
                                        except Exception as e:
                                            print(f"Error in _send_order: {e}")
                                            return None

                                    result = await self.loop.run_in_executor(None, _send_order)

                                    # result = await self.loop.run_in_executor(None, mt5.order_send, request)
                                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                                        print(f"✅ Successfully moved SL to {new_sl}")
                                    else:
                                        error = mt5.last_error()
                                        print(f"❌ Failed to move SL: {error}")
                            #     else:
                            #         print("SL would not move up - skipping update")
                            # else:
                            #     print("Not enough profit to move SL")


                        else:  # SELL position
                            profit = position.price_open - current_prices.ask
                            print(f"Current Profit: {profit}")
                            print(f"Minimum Profit Required: {min_profit}")

                            if profit > min_profit:  # Only trail if enough profit
                                new_sl = round(current_prices.ask + trailing_distance, symbol_info.digits)

                                if position.sl == 0 or new_sl < position.sl:
                                    print(f"📉 Moving SELL SL: {position.sl} → {new_sl} (Profit: {profit})")
                                    request = {
                                        "action": mt5.TRADE_ACTION_SLTP,
                                        "symbol": position.symbol,
                                        "position": position.ticket,
                                        "sl": new_sl,
                                        "tp": position.tp
                                    }

                                    result = await self.loop.run_in_executor(None, mt5.order_send, request)
                                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                                        print(f"✅ Successfully moved SL to {new_sl}")
                                    else:
                                        error = mt5.last_error()
                                        print(f"❌ Failed to move SL: {error}")

                    except Exception as e:
                        print(f"❌ Error processing position {position.ticket}: {e}")

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error monitoring trailing stops: {e}")
                await asyncio.sleep(1)



    async def _update_stop_loss_mt5(self, ticket: int, sl: float, tp: float, symbol: str) -> None:
        """Update position's stop loss with retry logic."""
        for attempt in range(5):  # 5 retry attempts
            try:
                def _modify():
                    request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "symbol": symbol,
                        "position": ticket,
                        "sl": sl,
                        "tp": tp,
                        "type_time": mt5.ORDER_TIME_GTC
                    }
                    
                    result = mt5.order_send(request)
                    success = result and result.retcode == mt5.TRADE_RETCODE_DONE
                    
                    if success:
                        print(f"✅ Updated trailing stop for #{ticket} to {sl}")
                    else:
                        error = mt5.last_error()
                        print(f"❌ Failed to update trailing stop (attempt {attempt+1}): {error}")
                        
                    return success

                success = await self.loop.run_in_executor(None, _modify)
                if success:
                    return
                    
                await asyncio.sleep(0.1 * (attempt + 1))  # Increasing delay between retries
                
            except Exception as e:
                logger.error(f"Error updating stop loss (attempt {attempt+1}): {e}")
                await asyncio.sleep(0.1 * (attempt + 1))

        logger.error(f"Failed to update trailing stop after 5 attempts")

    def cleanup(self):
        """Cleanup MT5 connection."""
        self.running = False  # Stop the monitor
        if self.initialized:
            mt5.shutdown()
            self.initialized = False


    async def async_update_position(self, trade_data: Dict[str, Any]) -> Dict[str, Any]:
        """Async wrapper to update position TP/SL in MT5."""
        if not self.loop:
            self.loop = asyncio.get_event_loop()
        return await self._retry_operation(
            lambda: self.loop.run_in_executor(None, self._update_position, trade_data)
        )
    
    def _get_position_error_message(self, result, request: Dict[str, Any]) -> str:
        """Get detailed error message based on MT5 return code and request type."""
        base_error = "Failed to update position: "
        
        # Common MT5 error codes
        if result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
            # Check which value was being modified
            if 'tp' in request and 'sl' not in request:
                return f"{base_error}Invalid TakeProfit level"
            elif 'sl' in request and 'tp' not in request:
                return f"{base_error}Invalid StopLoss level"
            else:
                return f"{base_error}Invalid TP/SL levels"
        
        # For other errors, return MT5's comment
        return f"{base_error}{result.comment}"

    async def _check_position_exists(self, ticket: int, symbol: str = None) -> Dict[str, Any]:
        """Centralized position check with detailed response."""
        if not self.loop:
            self.loop = asyncio.get_event_loop()
            
        def _check():
            try:
                if not self.initialized and not mt5.initialize():
                    return {"exists": False, "error": "MT5 initialization failed"}
                
                positions = mt5.positions_get(ticket=ticket)
                if not positions:
                    return {
                        "exists": False,
                        "error": f"Position #{ticket} not found"
                    }
                
                position = positions[0]
                
                # If symbol provided, verify it matches
                if symbol and position.symbol != symbol:
                    return {
                        "exists": False,
                        "error": f"Position #{ticket} exists but symbol mismatch: expected {symbol}, found {position.symbol}"
                    }
                
                return {
                    "exists": True,
                    "position": position,
                    "symbol": position.symbol,
                    "volume": position.volume,
                    "type": "buy" if position.type == mt5.POSITION_TYPE_BUY else "sell",
                    "price": position.price_open,
                    "tp": position.tp,
                    "sl": position.sl
                }
                
            except Exception as e:
                return {"exists": False, "error": str(e)}
        
        return await self.loop.run_in_executor(None, _check)

    def cleanup(self):
        """Cleanup MT5 connection."""
        if self.initialized:
            mt5.shutdown()
            self.initialized = False