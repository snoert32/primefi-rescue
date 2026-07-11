#!/usr/bin/env python3
"""
PrimeFi XDC Rescue Bot
======================
Monitort de PrimeFi XDC-pool en trekt AUTOMATISCH je deposit terug
zodra er liquiditeit vrijkomt. Herhaalt dit tot je volledige positie
is opgenomen. WXDC wordt automatisch ge-unwrapped naar native XDC.

Alle contractadressen zijn geverifieerd tegen de officiële PrimeFi-docs
en on-chain gecontroleerd (getReserveData bevestigt pWXDC).

VEREISTE ENV VARS:
  PRIVATE_KEY          private key van de wallet met de deposit (0x... of zonder prefix)
OPTIONELE ENV VARS:
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   → pushmeldingen via Telegram
  SLACK_WEBHOOK_URL                        → meldingen via Slack
  MIN_WITHDRAW_XDC     minimum liquiditeit om actie te ondernemen (default 50)
  POLL_SECONDS         polling-interval (default 15)
  DRY_RUN              zet op "1" om alleen te melden, niet te withdrawen

requirements.txt:  web3

VEILIGHEID:
  - De key wordt alleen gebruikt om withdraw/unwrap-transacties te signen.
  - Zodra je positie leeg is: verplaats je XDC naar een verse wallet en
    beschouw deze key als afgeschreven (hij heeft op een server gestaan).
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
from web3 import Web3
from eth_account import Account

# ── Geverifieerde contractadressen (PrimeFi v2, XDC mainnet, chain 50) ──
LENDING_POOL = "0x8a619D8E3BfAb54F7C30Ef39Ce16c53429c739C3"
WXDC         = "0x951857744785E80e2De051c32EE7b25f9c458C42"
PWXDC        = "0x1fF5E0037B478547715a4CE337d9fcFF86A30401"  # jouw deposit-token

RPC_URLS = [
    "https://erpc.xinfin.network",
    "https://rpc.xinfin.network",
    "https://rpc.xdcrpc.com",
]

# ── Config ──
MIN_WITHDRAW_XDC = float(os.environ.get("MIN_WITHDRAW_XDC", "50"))
POLL_SECONDS     = int(os.environ.get("POLL_SECONDS", "15"))
DRY_RUN          = os.environ.get("DRY_RUN", "0") == "1"
SAFETY_MARGIN    = 0.995   # neem 99.5% van beschikbare liquiditeit (race/rente-buffer)
DUST_XDC         = 1.0     # positie < 1 XDC = klaar

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SLACK_WEBHOOK_URL  = os.environ.get("SLACK_WEBHOOK_URL", "")

# ── ABIs (minimaal) ──
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "o", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]
POOL_ABI = [
    {"inputs": [{"name": "asset", "type": "address"},
                {"name": "amount", "type": "uint256"},
                {"name": "to", "type": "address"}],
     "name": "withdraw", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
]
WXDC_ABI = [
    {"constant": True, "inputs": [{"name": "o", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"inputs": [{"name": "wad", "type": "uint256"}], "name": "withdraw",
     "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]


def notify(text: str):
    print(text, flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = urllib.parse.urlencode(
                {"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        except Exception as e:
            print(f"  (telegram fout: {e})", flush=True)
    if SLACK_WEBHOOK_URL:
        try:
            req = urllib.request.Request(
                SLACK_WEBHOOK_URL,
                data=json.dumps({"text": text}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"  (slack fout: {e})", flush=True)


def connect() -> Web3:
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("Geen enkele XDC RPC bereikbaar")


def send_tx(w3: Web3, acct, fn, gas: int) -> dict:
    """Bouw, sign en verstuur een transactie; wacht op receipt."""
    tx = fn.build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": gas,
        "gasPrice": int(w3.eth.gas_price * 1.1),
        "chainId": 50,
    })
    signed = Account.sign_transaction(tx, acct.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return receipt


def main():
    pk = os.environ.get("PRIVATE_KEY", "").strip()
    if not pk:
        sys.exit("FOUT: zet PRIVATE_KEY als environment variable.")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    acct = Account.from_key(pk)

    w3 = connect()
    pool  = w3.eth.contract(address=Web3.to_checksum_address(LENDING_POOL), abi=POOL_ABI)
    wxdc  = w3.eth.contract(address=Web3.to_checksum_address(WXDC), abi=WXDC_ABI)
    pwxdc = w3.eth.contract(address=Web3.to_checksum_address(PWXDC), abi=ERC20_ABI)
    pwxdc_addr = Web3.to_checksum_address(PWXDC)
    wxdc_addr  = Web3.to_checksum_address(WXDC)

    deposit = pwxdc.functions.balanceOf(acct.address).call() / 1e18
    gas_bal = w3.eth.get_balance(acct.address) / 1e18
    mode = "DRY RUN (alleen melden)" if DRY_RUN else "AUTO-WITHDRAW ACTIEF"
    notify(f"🤖 PrimeFi Rescue Bot gestart | {mode}\n"
           f"Wallet: {acct.address[:10]}...\n"
           f"Positie: {deposit:,.0f} XDC | Gas-saldo: {gas_bal:.2f} XDC")

    if gas_bal < 2:
        notify("⚠️ Waarschuwing: minder dan 2 XDC voor gas. "
               "Stort wat XDC bij op de wallet, anders falen de transacties.")

    total_recovered = 0.0
    fail_streak = 0

    while True:
        try:
            deposit_wei = pwxdc.functions.balanceOf(acct.address).call()
            deposit = deposit_wei / 1e18

            if deposit < DUST_XDC:
                notify(f"✅ KLAAR! Volledige positie opgenomen. "
                       f"Totaal deze sessie: {total_recovered:,.2f} XDC.\n"
                       f"Advies: verplaats je XDC nu naar een verse wallet.")
                break

            liq_wei = wxdc.functions.balanceOf(pwxdc_addr).call()
            liq = liq_wei / 1e18
            print(f"[{time.strftime('%H:%M:%S')}] liquiditeit: {liq:,.2f} XDC | "
                  f"resterende positie: {deposit:,.2f} XDC", flush=True)

            if liq >= MIN_WITHDRAW_XDC:
                amount_wei = min(int(liq_wei * SAFETY_MARGIN), deposit_wei)
                amount = amount_wei / 1e18

                if DRY_RUN:
                    notify(f"🚨 [DRY RUN] {liq:,.0f} XDC beschikbaar — "
                           f"zou nu {amount:,.0f} XDC withdrawen.")
                    time.sleep(60)  # niet spammen in dry run
                    continue

                notify(f"🚨 {liq:,.0f} XDC liquiditeit gevonden! "
                       f"Withdraw van {amount:,.0f} XDC wordt verstuurd...")
                try:
                    r1 = send_tx(w3, acct,
                                 pool.functions.withdraw(wxdc_addr, amount_wei, acct.address),
                                 gas=600_000)
                    if r1["status"] != 1:
                        raise RuntimeError(f"withdraw reverted (tx {r1['transactionHash'].hex()})")

                    # Unwrap ontvangen WXDC → native XDC
                    wxdc_bal = wxdc.functions.balanceOf(acct.address).call()
                    if wxdc_bal > 0:
                        r2 = send_tx(w3, acct,
                                     wxdc.functions.withdraw(wxdc_bal), gas=100_000)
                        if r2["status"] != 1:
                            notify("⚠️ Withdraw gelukt maar unwrap faalde — "
                                   "je hebt WXDC (ERC-20) in je wallet, handmatig "
                                   "unwrappen kan altijd nog.")

                    total_recovered += amount
                    remaining = pwxdc.functions.balanceOf(acct.address).call() / 1e18
                    notify(f"✅ {amount:,.2f} XDC opgenomen en ge-unwrapped!\n"
                           f"Totaal gered: {total_recovered:,.2f} XDC | "
                           f"Nog in pool: {remaining:,.2f} XDC")
                    fail_streak = 0
                    time.sleep(3)
                    continue  # meteen opnieuw checken, er kan meer vrijkomen

                except Exception as e:
                    fail_streak += 1
                    msg = str(e)[:200]
                    print(f"  withdraw-poging faalde: {msg}", flush=True)
                    if fail_streak == 3:
                        notify(f"⚠️ 3 withdraw-pogingen op rij gefaald "
                               f"(laatste fout: {msg}). Iemand anders kaapt de "
                               f"liquiditeit weg, of er is een contractprobleem. "
                               f"Bot blijft proberen.")
                    time.sleep(2)
                    continue

            fail_streak = 0
        except Exception as e:
            print(f"RPC-fout ({str(e)[:120]}), opnieuw verbinden...", flush=True)
            try:
                w3 = connect()
                pool  = w3.eth.contract(address=Web3.to_checksum_address(LENDING_POOL), abi=POOL_ABI)
                wxdc  = w3.eth.contract(address=Web3.to_checksum_address(WXDC), abi=WXDC_ABI)
                pwxdc = w3.eth.contract(address=Web3.to_checksum_address(PWXDC), abi=ERC20_ABI)
            except Exception:
                pass

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
