"""Probe ExpTech chat completions models (run on Pi)."""
from __future__ import annotations

import asyncio
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv("/home/evan/kpp/.env")


async def try_model(model: str) -> None:
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(
            f"{os.getenv('AI_BASE_URL').rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {os.getenv('AI_API_KEY')}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            '只輸出這個 JSON 本身，不要其他文字：'
                            '{"summary":"ok","strengths":["a"],"weaknesses":["b"],'
                            '"next_run_goals":["c"],"lap_observations":[],'
                            '"confidence_score":50}'
                        ),
                    },
                    {
                        "role": "user",
                        "content": '{"car_number":"15","best_lap_time":51.96,"laps":[{"lap_number":1,"lap_time":52.1}]}',
                    },
                ],
                "max_tokens": 800,
                "temperature": 0.2,
            },
        )
        print("MODEL", model, "status", r.status_code)
        try:
            data = r.json()
        except Exception:
            print("non-json", r.text[:800])
            return
        print(json.dumps(data, ensure_ascii=False)[:2000])
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        print(
            "finish_reason=",
            choice.get("finish_reason"),
            "content_repr=",
            repr((msg.get("content") or "")[:200]),
            "reasoning=",
            repr(str(msg.get("reasoning") or msg.get("reasoning_content") or "")[:200]),
        )
        print("---")


async def main() -> None:
    models = [
        os.getenv("AI_AUTO_CHAT_MODEL"),
        os.getenv("AI_DEFAULT_MODEL", "auto"),
        os.getenv("AI_FAST_MODEL"),
        "ornith-1.0-9b",
        "ornith-1.0-35b-a3b",
        "auto",
    ]
    seen: set[str] = set()
    for m in models:
        if not m or m in seen:
            continue
        seen.add(m)
        try:
            await try_model(m)
        except Exception as exc:
            print("MODEL", m, "ERR", type(exc).__name__, exc)


asyncio.run(main())
