"""Prueba local del flujo completo con Deepgram, Claude y Google FALSOS (sin internet ni claves reales).

    python test_pipeline.py claude
    python test_pipeline.py google
"""
import asyncio
import json
import os
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "claude"
EXTRAS = MODE == "extras"       # nota de tema + segunda pasada
if EXTRAS:
    MODE = "claude"
FALLBACK = MODE == "fallback"   # Claude responde error; debe usarse Google
if FALLBACK:
    MODE = "claude"

os.environ["DEEPGRAM_API_KEY"] = "fake-dg"
os.environ["GOOGLE_TRANSLATE_API_KEY"] = "fake-google"
os.environ["ANTHROPIC_API_KEY"] = "fake-claude"
os.environ["TRANSLATOR"] = MODE
if EXTRAS:
    os.environ["TOPIC_EVERY"] = "2"
    os.environ["REFINE"] = "1"
    os.environ["REFINE_AFTER"] = "1"
    os.environ["REFINE_MIN_CHARS"] = "1"
os.environ["DEEPGRAM_URL"] = "ws://127.0.0.1:8791/v1/listen"
os.environ["TRANSLATE_URL"] = "http://127.0.0.1:8791/translate"
os.environ["ANTHROPIC_URL"] = "http://127.0.0.1:8791/v1/messages"
os.environ["MIN_LINE_CHARS"] = "10"
os.environ["HARD_LINE_CHARS"] = "60"
os.environ["SILENCE_FLUSH"] = "1.0"
# Contexto de clase opcional: "extras" usa la ficha de ejemplo; el resto corre sin ficha ni calendario.
_HERE = os.path.dirname(os.path.abspath(__file__))
_MISSING = os.path.join(_HERE, "no-existe.txt")
os.environ["FICHA_FILE"] = os.path.join(_HERE, "ficha.example.txt") if EXTRAS else _MISSING
os.environ["CALENDAR_FILE"] = _MISSING

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import server  # noqa: E402

received_audio = []
seen_auth = []
claude_calls = []
claude_headers = []
topic_calls = []
refine_calls = []


def res(text, final):
    return json.dumps(
        {"type": "Results", "is_final": final, "speech_final": False,
         "channel": {"alternatives": [{"transcript": text}]}}
    )


async def fake_listen(request):
    seen_auth.append(request.headers.get("Authorization"))
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    sent = False
    async for m in ws:
        if m.type == aiohttp.WSMsgType.BINARY:
            received_audio.append(len(m.data))
            if not sent:
                sent = True
                await ws.send_str(res("שלום", False))
                await ws.send_str(res("שלום לכולם.", True))        # >=10 y con punto -> línea 1
                await ws.send_str(res("היום נדבר על", True))       # sin punto: espera silencio
                await asyncio.sleep(1.8)                            # > SILENCE_FLUSH -> línea 2
                await ws.send_str(res("pressing גבוה. הבלמים " + "א" * 70, True))  # >60 sin punto final -> línea 3
    return ws


async def fake_google(request):
    body = await request.json()
    return web.json_response({"data": {"translations": [{"translatedText": "G:" + body["q"]}]}})


async def fake_claude(request):
    if FALLBACK:
        claude_calls.append("falla")
        return web.json_response({"error": {"message": "Your credit balance is too low"}}, status=400)
    claude_headers.append((request.headers.get("x-api-key"), request.headers.get("anthropic-version")))
    body = await request.json()
    user = body["messages"][0]["content"]
    if "<frases>" in user:
        topic_calls.append(user)
        return web.json_response({"content": [{"type": "text", "text": "NOTA: se habla de scouting"}]})
    claude_calls.append(body)
    if "<despues>" in user:
        refine_calls.append(user)
        t = user.split("<traducir>\n")[1].split("\n</traducir>")[0]
        return web.json_response({"content": [{"type": "text", "text": "R:" + t}]})
    text = user.split("<traducir>\n")[1].split("\n</traducir>")[0]
    return web.json_response({"content": [{"type": "text", "text": " C:" + text + " "}]})


async def main():
    mock = web.Application()
    mock.router.add_get("/v1/listen", fake_listen)
    mock.router.add_post("/translate", fake_google)
    mock.router.add_post("/v1/messages", fake_claude)
    r1 = web.AppRunner(mock)
    await r1.setup()
    await web.TCPSite(r1, "127.0.0.1", 8791).start()
    r2 = web.AppRunner(server.make_app())
    await r2.setup()
    await web.TCPSite(r2, "127.0.0.1", 8792).start()

    async with aiohttp.ClientSession() as s:
        cfg = await (await s.get("http://127.0.0.1:8792/config")).json()
        assert cfg["deepgram"] and cfg["translate"] and cfg["translator"] == MODE, cfg

        msgs = []
        async with s.ws_connect("http://127.0.0.1:8792/ws") as ws:
            await ws.send_json({"type": "start", "target": "es"})
            await asyncio.sleep(0.5)
            await ws.send_bytes(b"\x00\x01" * 1600)
            await asyncio.sleep(1.0)
            await ws.send_json({"type": "pause"})
            await ws.send_bytes(b"\x00\x01" * 1600)  # en pausa: no debe reenviarse
            await asyncio.sleep(0.3)
            await ws.send_json({"type": "resume"})
            await ws.send_bytes(b"\x00\x01" * 1600)
            await asyncio.sleep(3.0)
            while True:
                try:
                    m = await ws.receive(timeout=0.5)
                except asyncio.TimeoutError:
                    break
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                msgs.append(json.loads(m.data))

    finals = [m for m in msgs if m["kind"] == "final"]
    trs = [m for m in msgs if m["kind"] == "translation"]
    print("Líneas finales:", [f["he"] for f in finals])
    print("Traducciones:", [t["tr"] for t in trs])
    if EXTRAS:
        assert len(finals) == 3, finals
        # Segunda pasada: las frases 1 y 2 se reintentan con la siguiente; la última no tiene "después".
        assert len(refine_calls) == 2, refine_calls
        assert "<despues>" in refine_calls[0] and "<ya_traducida>" in refine_calls[0]
        assert "C:" + finals[0]["he"] in refine_calls[0]
        final_tr = {}
        for t in trs:
            final_tr[t["id"]] = t["tr"]          # la última versión de cada id es la que queda en pantalla
        assert final_tr[finals[0]["id"]] == "R:" + finals[0]["he"], final_tr
        assert final_tr[finals[2]["id"]] == "C:" + finals[2]["he"], final_tr
        # Nota de tema: se pide cada 2 frases traducidas y se manda en las frases siguientes.
        assert len(topic_calls) >= 1 and "C:" + finals[0]["he"] in topic_calls[0], topic_calls
        assert any("<tema_actual>\nNOTA: se habla de scouting" in c["messages"][0]["content"] for c in claude_calls), "tema"
        assert "Ficha de la clase" in claude_calls[0]["system"]
        print("OK (extras): nota de tema, ficha y segunda pasada funcionan.")
        await r2.cleanup()
        await r1.cleanup()
        return
    assert len(finals) == 3, finals
    assert [t["id"] for t in trs] == [f["id"] for f in finals], "orden"
    assert all(not t.get("error") for t in trs), trs
    assert seen_auth == ["Token fake-dg"], seen_auth

    if FALLBACK:
        assert all(t["tr"].startswith("G:") for t in trs), trs
        statuses = [m["msg"] for m in msgs if m["kind"] == "status" and "Claude" in m["msg"]]
        assert len(statuses) == 1 and "credit balance" in statuses[0], statuses
        assert claude_calls == ["falla"], claude_calls   # solo 1 intento: después usa Google 60 s
        assert len(received_audio) == 2, received_audio
        print("OK (fallback): Claude falla, se usa Google, se avisa una vez y no se insiste.")
        await r2.cleanup()
        await r1.cleanup()
        return
    if MODE == "claude":
        assert [t["tr"] for t in trs] == ["C:" + f["he"] for f in finals], trs  # sin espacios de más
        assert all(h == ("fake-claude", "2023-06-01") for h in claude_headers), claude_headers
        b = claude_calls[0]
        assert b["model"] == "claude-haiku-4-5" and b["temperature"] == 0
        assert "español" in b["system"]
        assert "Ficha de la clase" not in b["system"]      # sin ficha ni calendario: funciona igual
        assert "Glosario" not in b["system"]               # frase sin términos: no se manda glosario
        assert "Glosario" in claude_calls[2]["system"] and "בלם = central" in claude_calls[2]["system"]
        assert "ימאל" not in claude_calls[2]["system"]      # solo lo que aparece en la frase
        assert "<contexto_previo>" not in b["messages"][0]["content"]        # primera línea: sin contexto
        assert "<contexto_previo>" in claude_calls[1]["messages"][0]["content"]
        assert "שלום לכולם. => C:שלום לכולם." in claude_calls[1]["messages"][0]["content"]
        assert claude_calls[2]["messages"][0]["content"].count(" => ") == 2   # 2 frases de contexto
    else:
        assert all(t["tr"].startswith("G:") for t in trs), trs
        assert not claude_calls

    assert len(received_audio) == 2, received_audio
    print(f"OK ({MODE}): corte, orden, contexto/traductor, pausa y autenticación funcionan.")
    await r2.cleanup()
    await r1.cleanup()


asyncio.run(main())