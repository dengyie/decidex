#!/usr/bin/env python3
"""
Automated full-game runner for Balatro using DecideX advisor.
Drives the game step-by-step through blinds, rounds, hands, and shop until completion.
"""

import os
import sys
import time
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

BASE_URL = os.environ.get("BALATRO_WEB_URL", "http://127.0.0.1:8091")
USER = os.environ.get("BALATRO_WEB_USER", "mango")
PASS = os.environ.get("BALATRO_WEB_PASSWORD", "12345678")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

class BalatroRunner:
    def __init__(self, base_url: str = BASE_URL, kind: str = "heuristic"):
        self.base_url = base_url
        self.kind = kind
        self.cookie: Optional[str] = None
        self.stats = {
            "hands_played": 0,
            "discards_used": 0,
            "blinds_cleared": 0,
            "shop_buys": [],
            "plays_detail": [],
            "highest_score": 0.0,
            "final_ante": 1,
            "final_round": 1,
            "final_status": "in_progress",
            "start_time": time.time(),
        }

    def login(self) -> bool:
        url = f"{self.base_url}/api/login"
        data = json.dumps({"user": USER, "password": PASS}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "X-Requested-With": "decidex-web"}
        )
        try:
            with urllib.request.urlopen(req) as resp:
                cookie_hdr = resp.headers.get("Set-Cookie")
                if cookie_hdr:
                    self.cookie = cookie_hdr.split(";")[0]
                    logging.info("Logged in successfully to Balatro web API.")
                    return True
        except Exception as e:
            logging.error(f"Login failed: {e}")
        return False

    def get_live_state(self) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/api/game?name=live&_t={int(time.time() * 1000)}"
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logging.warning(f"Failed to fetch live state: {e}")
            return None

    def execute_action(self, action_type: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}/api/execute"
        payload = {"action_type": action_type, "kind": self.kind, **kwargs}
        headers = {"Content-Type": "application/json", "X-Requested-With": "decidex-web"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logging.error(f"Execute {action_type} error: {e}")
            return {"ok": False, "error": str(e)}

    def run_game(self, max_steps: int = 1500):
        logging.info("================ Starting Full DecideX Balatro Run ================")
        if not self.login():
            logging.error("Cannot proceed without login.")
            return

        stuck_state = None
        stuck_count = 0
        step = 0
        started = False

        while step < max_steps:
            step += 1
            state = self.get_live_state()
            if not state:
                time.sleep(1)
                continue

            st_name = state.get("state_name", "unknown")
            ante = state.get("ante", 1)
            round_num = state.get("round", 1)
            money = state.get("money", 0)
            hands_left = state.get("hands_left", 0)
            discards_left = state.get("discards_left", 0)
            blind = state.get("blind", {})
            b_name = blind.get("name", "Unknown")
            b_target = blind.get("chips", 0)
            b_scored = blind.get("scored", 0)

            self.stats["final_ante"] = ante
            self.stats["final_round"] = round_num

            # Detect stuck loop
            state_sig = (st_name, ante, round_num, hands_left, discards_left, b_scored)
            if state_sig == stuck_state:
                stuck_count += 1
                if stuck_count >= 10:
                    logging.warning(f"Potentially stuck in {st_name} for {stuck_count} checks. Pumping action if ready...")
                    if st_name == "SELECTING_HAND" and hands_left > 0 and (b_target <= 0 or b_scored < b_target):
                        self.execute_action("auto")
                    elif st_name == "BLIND_SELECT" and state.get("can_select_blind"):
                        self.execute_action("select_blind")
                    elif st_name == "ROUND_EVAL" and state.get("can_cash_out"):
                        self.execute_action("cash_out")
                    elif st_name == "SHOP" and state.get("can_toggle_shop"):
                        self.execute_action("toggle_shop")
                    stuck_count = 0
            else:
                stuck_state = state_sig
                stuck_count = 0

            # Victory check
            if state.get("won") or st_name in ("WIN", "VICTORY"):
                logging.info(f"🎉 VICTORY! Ante {ante} Defeated! Game Won!")
                self.stats["final_status"] = "victory"
                break

            # State Dispatcher
            if st_name in ("MENU", "SPLASH"):
                if not started:
                    logging.info(f"Game in {st_name}. Initiating start_run...")
                    res = self.execute_action("start_run")
                    logging.info(f"start_run: {res.get('description')}")
                    started = True
                    time.sleep(3)
                else:
                    logging.info("Returned to MENU after playing. Run completed!")
                    self.stats["final_status"] = "finished_at_menu"
                    break

            elif st_name == "BLIND_SELECT":
                started = True
                can_sel = state.get("can_select_blind", False)
                if not can_sel:
                    logging.info("Blind select screen animating... waiting")
                    time.sleep(1.2)
                else:
                    logging.info(f"--- [Ante {ante} | Round {round_num}] Blind Select: {b_name} (${money}) ---")
                    res = self.execute_action("select_blind")
                    logging.info(f"Action: {res.get('description', 'select_blind')}")
                    time.sleep(2.5)

            elif st_name in ("DRAW_TO_HAND", "HAND_PLAYED"):
                logging.info(f"Balatro animating ({st_name})... waiting")
                time.sleep(1.8)

            elif st_name == "SELECTING_HAND":
                started = True
                hand_cards = state.get("hand", [])
                hand_str = " ".join(f"{c.get('rank')}{c.get('suit')}" for c in hand_cards)

                if b_target > 0 and b_scored >= b_target:
                    logging.info(f"[Ante {ante} R{round_num}] {b_name} ({b_scored}/{b_target}) | Target cleared! Waiting for evaluation...")
                    time.sleep(1.5)
                    continue

                if hands_left <= 0 or len(hand_cards) == 0:
                    logging.info(f"[Ante {ante} R{round_num}] {b_name} | Waiting for round finish (hands: {hands_left}, cards: {len(hand_cards)})...")
                    time.sleep(1.5)
                    continue

                logging.info(f"[Ante {ante} R{round_num}] {b_name} ({b_scored}/{b_target}) | Hands: {hands_left}, Discards: {discards_left} | Hand: [{hand_str}]")
                res = self.execute_action("auto")
                if res.get("ok"):
                    desc = res.get("description", "")
                    act_type = res.get("action_type")
                    logging.info(f"  -> DecideX Decision: [{act_type}] {desc}")
                    if act_type == "play":
                        self.stats["hands_played"] += 1
                        rec = res.get("recommendation", {})
                        exp_score = rec.get("expected_score") or 0.0
                        if exp_score > self.stats["highest_score"]:
                            self.stats["highest_score"] = exp_score
                        self.stats["plays_detail"].append({
                            "round": round_num,
                            "desc": desc,
                            "exp_score": exp_score,
                        })
                    elif act_type == "discard":
                        self.stats["discards_used"] += 1
                else:
                    logging.warning(f"  -> Auto decision returned not ok: {res.get('error')}")
                time.sleep(3.2)

            elif st_name == "ROUND_EVAL":
                can_cash_out = state.get("can_cash_out", False)
                if not can_cash_out:
                    logging.info(f"🏆 Round {round_num} Cleared! (${money} + rewards). Animating evaluation... waiting")
                    time.sleep(1.2)
                else:
                    logging.info(f"🏆 Round {round_num} Evaluation complete. Cashing out...")
                    self.stats["blinds_cleared"] += 1
                    res = self.execute_action("cash_out")
                    logging.info(f"Cash out: {res.get('description')}")
                    time.sleep(2.5)

            elif st_name == "SHOP":
                can_toggle = state.get("can_toggle_shop", False)
                if not can_toggle:
                    logging.info("🛒 Shop entrance animating / not ready to toggle... waiting")
                    time.sleep(1.2)
                    continue

                shop_info = state.get("shop", {})
                cards = shop_info.get("cards", [])
                logging.info(f"🛒 In Shop! Money: ${money}. Available cards: {[c.get('name') + '($' + str(c.get('cost')) + ')' for c in cards]}")
                jokers_count = len(state.get("jokers", []))
                if jokers_count < 5 and cards:
                    for idx, c in enumerate(cards):
                        cost = c.get("cost", 99)
                        c_type = c.get("set", "")
                        if money >= cost and c_type in ("Joker", "joker"):
                            logging.info(f"  -> Buying {c.get('name')} for ${cost} (index {idx})")
                            buy_res = self.execute_action("buy_card", index=idx, area="shop_jokers")
                            logging.info(f"  -> Buy result: {buy_res}")
                            self.stats["shop_buys"].append(c.get("name"))
                            money -= cost
                            time.sleep(1.5)
                            break
                logging.info("Leaving shop (toggle_shop)...")
                res = self.execute_action("toggle_shop")
                logging.info(f"Toggle shop: {res.get('description')}")
                time.sleep(2.5)

            elif st_name == "GAME_OVER":
                logging.info(f"💀 GAME OVER reached at Ante {ante}, Round {round_num}.")
                self.stats["final_status"] = "game_over"
                break

            elif st_name in ("WIN", "VICTORY"):
                logging.info(f"🎉 VICTORY! Ante {ante} Defeated!")
                self.stats["final_status"] = "victory"
                break

            else:
                logging.info(f"State: {st_name}. Waiting...")
                time.sleep(1.5)

        elapsed = round(time.time() - self.stats["start_time"], 1)
        self.stats["total_time_seconds"] = elapsed
        self.stats["total_steps"] = step
        logging.info("================ DecideX Full Run Finished ================")
        logging.info(f"Final Status: {self.stats['final_status']}")
        logging.info(f"Final Ante: {self.stats['final_ante']} | Final Round: {self.stats['final_round']}")
        logging.info(f"Total time: {elapsed}s | Total Steps: {step}")
        logging.info(f"Blinds Cleared: {self.stats['blinds_cleared']}")
        logging.info(f"Hands Played: {self.stats['hands_played']}")
        logging.info(f"Discards Used: {self.stats['discards_used']}")
        logging.info(f"Highest Projected Hand: {self.stats['highest_score']} pts")
        logging.info(f"Shop Jokers Purchased: {self.stats['shop_buys']}")
        
        try:
            with open("full_game_report.json", "w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
            logging.info("Saved battle report to full_game_report.json")
        except Exception as e:
            logging.warning(f"Failed to save report: {e}")

        print(json.dumps(self.stats, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    runner = BalatroRunner()
    runner.run_game()
