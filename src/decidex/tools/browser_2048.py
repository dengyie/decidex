"""
Browser automation adapter for 2048 on https://game.ark717.com/ via Chrome CDP (port 9222).
Provides non-invasive lifecycle hooks, board extraction, and zero-latency move dispatch.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
import websockets


class Chrome2048Controller:
    """
    Controls 2048 running in Chrome via DevTools Protocol.
    """

    def __init__(
        self,
        cdp_port: int = 9222,
        tab_url_pattern: str = "game.ark717.com",
        ws_url: Optional[str] = None
    ):
        self.cdp_port = cdp_port
        self.tab_url_pattern = tab_url_pattern
        self.ws_url = ws_url
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._listen_task: Optional[asyncio.Task] = None

    @classmethod
    def find_tab_ws_url(cls, cdp_port: int = 9222, pattern: str = "game.ark717.com") -> str:
        """Finds the webSocketDebuggerUrl of the target tab from http://127.0.0.1:port/json."""
        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=3.0) as resp:
            tabs = json.loads(resp.read().decode())
        for t in tabs:
            url = t.get("url", "")
            title = t.get("title", "")
            if pattern in url or "2048" in title or "游戏" in title:
                ws = t.get("webSocketDebuggerUrl")
                if ws:
                    return ws
        raise RuntimeError(f"Target tab matching '{pattern}' not found on port {cdp_port}")

    async def connect(self) -> None:
        """Establishes WebSocket connection to the tab CDP endpoint."""
        if not self.ws_url:
            self.ws_url = self.find_tab_ws_url(self.cdp_port, self.tab_url_pattern)
        self._ws = await websockets.connect(self.ws_url)
        self._listen_task = asyncio.create_task(self._listener())

        # Bring tab to front to avoid RAF background throttling
        await self.call("Page.bringToFront")

    async def _listener(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id in self._pending:
                    fut = self._pending.get(msg_id)
                    if fut and not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            # Socket closed or disconnected
            pass
        finally:
            err = ConnectionError("CDP WebSocket connection closed")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Calls a CDP method and awaits the matching response id."""
        if not self._ws or self._ws.close_code is not None:
            raise ConnectionError("CDP WebSocket is not connected")
        msg_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(json.dumps({
                "id": msg_id,
                "method": method,
                "params": params or {}
            }))
            res = await fut
            return res
        finally:
            self._pending.pop(msg_id, None)

    async def eval(self, expr: str) -> Any:
        """Evaluates JavaScript expression in tab context and returns value."""
        res = await self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True
        })
        return res.get("result", {}).get("result", {}).get("value")

    async def setup_hooks(self) -> Dict[str, Any]:
        """
        Installs non-invasive factory and canvas interceptor on ArcadeGames["2048"].
        """
        inject_script = """
        (() => {
            window.__decidex = window.__decidex || {
                grid: Array.from({length: 4}, () => Array(4).fill(0)),
                buildingGrid: Array.from({length: 4}, () => Array(4).fill(0)),
                score: 0,
                dead: false,
                moves: 0,
                gameInst: null
            };
            
            if (!window.__origG2048Factory && window.ArcadeGames && window.ArcadeGames["2048"]) {
                window.__origG2048Factory = window.ArcadeGames["2048"].factory;
                
                window.ArcadeGames["2048"].factory = function(ctx, W, H, api) {
                    const wrappedApi = Object.assign({}, api, {
                        setScore(n) {
                            window.__decidex.score = n;
                            if (api && api.setScore) api.setScore(n);
                        },
                        gameOver(score) {
                            window.__decidex.dead = true;
                            window.__decidex.score = score;
                            if (api && api.gameOver) api.gameOver(score);
                        }
                    });
                    
                    const origSave = ctx.save;
                    const origRestore = ctx.restore;
                    const origFillText = ctx.fillText;
                    let saveDepth = 0;
                    
                    ctx.save = function() {
                        if (saveDepth === 0) {
                            window.__decidex.buildingGrid = Array.from({length: 4}, () => Array(4).fill(0));
                        }
                        saveDepth++;
                        return origSave.apply(this, arguments);
                    };
                    
                    ctx.restore = function() {
                        saveDepth--;
                        if (saveDepth <= 0) {
                            saveDepth = 0;
                            window.__decidex.grid = window.__decidex.buildingGrid;
                        }
                        return origRestore.apply(this, arguments);
                    };
                    
                    ctx.fillText = function(text, x, y, maxW) {
                        const val = parseInt(text, 10);
                        if (saveDepth > 0 && !isNaN(val) && val > 0 && (val & (val - 1)) === 0) {
                            const col = Math.round((x - 65.25) / 116.5);
                            const row = Math.round((y - 187.25) / 116.5);
                            if (row >= 0 && row < 4 && col >= 0 && col < 4) {
                                window.__decidex.buildingGrid[row][col] = val;
                            }
                        }
                        return origFillText.apply(this, arguments);
                    };
                    
                    const inst = window.__origG2048Factory.call(this, ctx, W, H, wrappedApi);
                    window.__decidex.gameInst = inst;
                    return inst;
                };
            }
            
            return {
                installed: true,
                hasFactory: !!(window.ArcadeGames && window.ArcadeGames["2048"])
            };
        })()
        """
        return await self.eval(inject_script)

    async def start_practice_mode(self) -> Dict[str, Any]:
        """
        Navigates into 2048 game modal and clicks '练习模式'.
        """
        start_script = """
        (() => {
            // Reset decidex state flags for new session
            if (window.__decidex) {
                window.__decidex.dead = false;
            }

            // If gamewrap is not open, click 2048 card
            const wrap = document.getElementById("gamewrap");
            if (!wrap || !wrap.classList.contains("open")) {
                const card = document.querySelector('.cab[data-game="2048"]');
                if (card) card.click();
            }
            
            // Click practice mode button
            const btn = document.getElementById("mPractice");
            if (btn) {
                btn.click();
                return { started: true, clicked: true };
            }
            return { started: true, clicked: false, open: true };
        })()
        """
        res = await self.eval(start_script)
        await asyncio.sleep(0.2)
        return res

    async def get_state(self) -> Dict[str, Any]:
        """
        Reads the real-time 4x4 matrix, score, game over state, and move count.
        """
        state_script = """
        (() => {
            const hudScore = document.getElementById("hudScore");
            const hudVal = hudScore ? parseInt(hudScore.innerText.replace(/[^0-9]/g, ""), 10) || 0 : 0;
            
            const dx = window.__decidex || {};
            const grid = dx.grid || Array.from({length: 4}, () => Array(4).fill(0));
            const score = Math.max(dx.score || 0, hudVal);
            const dead = !!dx.dead;
            
            return {
                grid: grid,
                score: score,
                dead: dead,
                moves: dx.moves || 0,
                hasInst: !!dx.gameInst
            };
        })()
        """
        return await self.eval(state_script)

    async def send_move(self, direction: str) -> bool:
        """
        Dispatches a move ('UP', 'DOWN', 'LEFT', 'RIGHT') via direct onKey dispatch,
        and triggers a synchronous draw frame to eliminate render desynchronization.
        """
        key_map = {
            "LEFT": "ArrowLeft",
            "RIGHT": "ArrowRight",
            "UP": "ArrowUp",
            "DOWN": "ArrowDown",
            "ArrowLeft": "ArrowLeft",
            "ArrowRight": "ArrowRight",
            "ArrowUp": "ArrowUp",
            "ArrowDown": "ArrowDown"
        }
        key_str = key_map.get(direction, direction)
        move_script = f"""
        (() => {{
            if (window.__decidex && window.__decidex.gameInst) {{
                window.__decidex.gameInst.onKey({{ key: "{key_str}" }});
                window.__decidex.moves = (window.__decidex.moves || 0) + 1;
                // Force synchronous draw to flush canvas text capture immediately
                if (typeof window.__decidex.gameInst.draw === "function") {{
                    try {{
                        window.__decidex.gameInst.draw();
                    }} catch (e) {{}}
                }}
                return true;
            }}
            window.dispatchEvent(new KeyboardEvent("keydown", {{ key: "{key_str}", code: "{key_str}", bubbles: true }}));
            return true;
        }})()
        """
        res = await self.eval(move_script)
        return bool(res)

    async def send_move_and_wait_settlement(
        self,
        direction: str,
        initial_grid: Optional[List[List[int]]] = None,
        timeout_s: float = 0.12
    ) -> Dict[str, Any]:
        """
        Dispatches move and waits until the board grid changes or the game reports dead,
        guaranteeing strict state settlement and eliminating ghost polling on stale frames.
        """
        await self.send_move(direction)
        t_start = time.perf_counter()
        while time.perf_counter() - t_start < timeout_s:
            state = await self.get_state()
            if state.get("dead"):
                return state
            if initial_grid is not None and state.get("grid") != initial_grid:
                return state
            await asyncio.sleep(0.015)
        return await self.get_state()

    async def close(self) -> None:
        """Closes the WebSocket connection and cleans up pending futures."""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except (asyncio.CancelledError, Exception):
                pass
            self._listen_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        err = ConnectionError("CDP WebSocket connection closed")
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def __aenter__(self) -> Chrome2048Controller:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
