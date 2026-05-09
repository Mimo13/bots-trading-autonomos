#!/usr/bin/env python3
"""
AI-Advisor — Filtro de señales de trading mediante LLM (GitHub Copilot / OpenAI).
Cada bot puede consultar si una señal debe ejecutarse o rechazarse.

Config: ai_advisor_config.json
"""
from __future__ import annotations
import json, logging, os, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent

# Cache simple en memoria: dict key -> (timestamp, respuesta)
_cache: dict = {}
_cache_ttl: int = 120  # segundos

_logger = logging.getLogger('ai_advisor')


def _load_config() -> dict:
    cfg_path = ROOT / 'ai_advisor_config.json'
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
    else:
        cfg = {}
    defaults = {
        'enabled': False,
        'provider': 'github_copilot',
        'model': 'gpt-4.1',
        'temperature': 0.1,
        'max_tokens': 200,
        'decision_threshold': 0.6,
        'max_calls_per_minute': 10,
        'max_cost_per_day_usd': 0.50,
        'cache_seconds': 120,
        'fallback_on_error': True,
        'log_all_decisions': True,
        'timeout_seconds': 5,
    }
    defaults.update(cfg)
    return defaults


def _get_token() -> str:
    """Obtener token de GitHub Copilot via gh CLI."""
    try:
        result = subprocess.run(
            ['gh', 'auth', 'token'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        _logger.warning(f"gh auth token failed: {e}")
    return ''


def _call_llm(system_prompt: str, user_message: str, cfg: dict) -> Optional[dict]:
    """Llamar al LLM y devolver respuesta JSON."""
    token = _get_token()
    if not token:
        _logger.warning("No GitHub token available")
        return None

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    payload = {
        'model': cfg.get('model', 'gpt-4.1'),
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message},
        ],
        'temperature': cfg.get('temperature', 0.1),
        'max_tokens': cfg.get('max_tokens', 200),
        'response_format': {'type': 'json_object'},
    }

    try:
        resp = requests.post(
            'https://api.githubcopilot.com/chat/completions',
            headers=headers,
            json=payload,
            timeout=cfg.get('timeout_seconds', 5),
        )
        resp.raise_for_status()
        data = resp.json()
        content = data['choices'][0]['message']['content']

        # Parsear la respuesta JSON
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            _logger.warning(f"LLM returned non-JSON: {content[:200]}")
            return None

        # Validar campos requeridos
        if 'decision' not in result:
            result['decision'] = 'REJECT'
        if 'confidence' not in result:
            result['confidence'] = 0.0
        if 'reason' not in result:
            result['reason'] = 'Respuesta mal formateada'

        return result

    except requests.exceptions.Timeout:
        _logger.warning("LLM timeout")
        return None
    except Exception as e:
        _logger.warning(f"LLM call failed: {e}")
        return None


SYSTEM_PROMPT = """Eres un asesor de trading automático. Tu función es validar o rechazar señales de trading basándote en el contexto de mercado proporcionado.

Responde ÚNICAMENTE con un JSON con esta estructura exacta:
{"decision": "EXECUTE" | "REJECT", "confidence": 0.0-1.0, "reason": "explicación breve (máx 30 palabras)"}

REGLAS:
- Si la señal es clara y el contexto la respalda, responde EXECUTE.
- Si hay señales contradictorias (ej. bullish pero RSI cerca de sobrecompra), responde REJECT.
- Si el mercado está lateral o la volatilidad es baja, prefiere REJECT.
- Si la dirección es clara, el riesgo controlado y las confirmaciones están alineadas, prefiere EXECUTE.
- No añadas texto fuera del JSON."""


def _build_user_message(signal: dict) -> str:
    """Construir el mensaje de usuario con los datos de la señal."""
    lines = []
    lines.append(f"Symbol: {signal.get('symbol', 'UNKNOWN')}")
    lines.append(f"Direction: {signal.get('direction', 'UNKNOWN')}")
    lines.append(f"Bot confidence: {signal.get('confidence', 0.5)}")
    lines.append(f"Price: ${signal.get('price', 0)}")
    lines.append(f"Reason: {signal.get('reason', 'N/A')}")

    ctx = signal.get('context', {})
    if ctx:
        for k, v in ctx.items():
            if isinstance(v, float):
                lines.append(f"{k}: {v:.4f}")
            else:
                lines.append(f"{k}: {v}")

    return '\n'.join(lines)


def _cache_key(signal: dict) -> str:
    """Generar clave de caché basada en símbolo, dirección y precio redondeado."""
    price_bucket = round(signal.get('price', 0) * 100)
    return f"{signal.get('symbol','')}:{signal.get('direction','')}:{price_bucket}"


def _rate_limited(cfg: dict) -> bool:
    """Comprobar rate limiting por minuto."""
    now = time.time()
    # Log simple de timestamps de llamadas
    log_dir = ROOT / 'runtime' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    rate_file = log_dir / 'ai_advisor_calls.log'

    # Leer llamadas recientes
    recent = []
    if rate_file.exists():
        content = rate_file.read_text()
        for line in content.strip().split('\n')[-100:]:
            if line:
                try:
                    t = float(line.strip())
                    if now - t < 60:
                        recent.append(t)
                except ValueError:
                    pass

    max_calls = cfg.get('max_calls_per_minute', 10)
    if len(recent) >= max_calls:
        _logger.info(f"Rate limit: {len(recent)} calls in last 60s (max {max_calls})")
        return True

    # Registrar nueva llamada
    with rate_file.open('a') as f:
        f.write(f"{now}\n")

    return False


def validate_signal(signal: dict) -> dict:
    """
    Validar una señal de trading usando el LLM.

    Args:
        signal: dict con keys:
            - symbol (str): Ej. 'SOLUSDT'
            - direction (str): 'BUY' | 'SELL' | 'SHORT' | 'COVER'
            - confidence (float): 0-1 confianza del bot
            - price (float): Precio actual
            - reason (str): Razón de la señal
            - context (dict, opcional): Indicadores técnicos

    Returns:
        dict: {'action': 'EXECUTE' | 'REJECT', 'confidence': 0.0-1.0, 'reason': str}
    """
    cfg = _load_config()

    if not cfg.get('enabled', False):
        return {'action': 'EXECUTE', 'confidence': 1.0, 'reason': 'AI-Advisor desactivado'}

    if not signal.get('price') or signal.get('price', 0) <= 0:
        return {'action': 'EXECUTE', 'confidence': 1.0, 'reason': 'Sin precio válido'}

    # Cache check
    ck = _cache_key(signal)
    now = time.time()
    if ck in _cache:
        cached_time, cached_result = _cache[ck]
        if now - cached_time < cfg.get('cache_seconds', 120):
            return cached_result

    # Rate limiting
    if _rate_limited(cfg):
        return {'action': 'EXECUTE', 'confidence': 0.7, 'reason': 'AI-Advisor rate limited'}

    # Llamar al LLM
    user_msg = _build_user_message(signal)
    result = _call_llm(SYSTEM_PROMPT, user_msg, cfg)

    if result is None:
        if cfg.get('fallback_on_error', True):
            return {'action': 'EXECUTE', 'confidence': 0.5, 'reason': 'AI-Advisor fallback (error)'}
        return {'action': 'REJECT', 'confidence': 0.0, 'reason': 'AI-Advisor error sin fallback'}

    # Mapear 'decision' -> 'action'
    decision = result.get('decision', 'EXECUTE')
    confidence = result.get('confidence', 0.5)
    reason = result.get('reason', 'Sin explicación')

    action = 'EXECUTE' if decision == 'EXECUTE' else 'REJECT'

    # Threshold: si confidence del LLM < threshold, rechazar
    if action == 'EXECUTE' and confidence < cfg.get('decision_threshold', 0.6):
        action = 'REJECT'
        reason = f"Confianza ({confidence:.2f}) por debajo del umbral"

    out = {'action': action, 'confidence': confidence, 'reason': reason}

    # Log every decision to CSV
    if cfg.get('log_all_decisions', True):
        log_dir = ROOT / 'runtime' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'ai_advisor_validations.csv'
        import csv
        log_exists = log_file.exists()
        with log_file.open('a', newline='') as f:
            w = csv.writer(f)
            if not log_exists:
                w.writerow(['timestamp_utc', 'symbol', 'direction', 'price', 'decision', 'confidence', 'reason'])
            w.writerow([
                datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                signal.get('symbol', ''),
                signal.get('direction', ''),
                signal.get('price', 0),
                action,
                round(confidence, 2),
                reason[:100],
            ])

    # Guardar en caché
    _cache[ck] = (now, out)

    return out


# ── Tests rápidos ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    signal = {
        'symbol': 'SOLUSDT',
        'direction': 'BUY',
        'confidence': 0.72,
        'price': 93.45,
        'reason': 'BULL_PULLBACK_PRICE_ABOVE_EMA',
        'context': {
            'ema_fast': 93.12,
            'ema_slow': 92.08,
            'rsi_7': 58.2,
            'atr_14': 1.18,
            'adx_14': 24.5,
            'volume_ratio': 1.35,
        }
    }
    # Forzar enabled para test
    cfg_path = ROOT / 'ai_advisor_config.json'
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        cfg['enabled'] = True
        cfg_path.write_text(json.dumps(cfg, indent=2))

    print("Señal:", json.dumps(signal, indent=2))
    print("\nValidando...")
    result = validate_signal(signal)
    print("\nResultado:", json.dumps(result, indent=2))

    # Restaurar
    cfg['enabled'] = False
    cfg_path.write_text(json.dumps(cfg, indent=2))
