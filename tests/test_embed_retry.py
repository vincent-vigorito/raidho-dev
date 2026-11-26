"""_http_post_json: riprova su 429/503 con l'attesa indicata dal provider. Nessuna rete."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import embed_providers  # noqa: E402

URL = 'https://openrouter.ai/api/v1/embeddings'
LIMITE = {'error': {'message': 'Retry the request after 2 sec', 'code': 429,
                    'metadata': {'retry_after_seconds': 2}}}


def provider_finto(monkeypatch, risposte):
    chiamate, attese = [], []

    def gestore(request):
        chiamate.append(request)
        return risposte[min(len(chiamate), len(risposte)) - 1]

    client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(gestore), **kw))
    monkeypatch.setattr(embed_providers.time, 'sleep', attese.append)
    return chiamate, attese


def test_429_con_retry_after_poi_200(monkeypatch):
    chiamate, attese = provider_finto(monkeypatch, [
        httpx.Response(429, json=LIMITE),
        httpx.Response(200, json={'data': [{'embedding': [0.1, 0.2]}]}),
    ])
    data = embed_providers._http_post_json(URL, {}, {'model': 'm', 'input': ['x']})
    assert data == {'data': [{'embedding': [0.1, 0.2]}]}
    assert len(chiamate) == 2
    assert attese == [2.0]


def test_429_a_oltranza_errore_dopo_i_tentativi(monkeypatch):
    chiamate, attese = provider_finto(monkeypatch, [httpx.Response(429, headers={'Retry-After': '120'}, json=LIMITE)])
    with pytest.raises(RuntimeError, match='HTTP 429 from'):
        embed_providers._http_post_json(URL, {}, {'model': 'm', 'input': ['x']})
    assert len(chiamate) == embed_providers._RIPROVA_TENTATIVI
    assert attese == [embed_providers._RIPROVA_MAX_S] * (embed_providers._RIPROVA_TENTATIVI - 1)


def test_retry_after_corto_attese_crescenti_fino_al_tetto(monkeypatch):
    # Nebius dichiara 2 s ma il limite dura di piu': le attese crescono oltre quella dichiarata
    chiamate, attese = provider_finto(monkeypatch, [httpx.Response(429, json=LIMITE)] * 8
                                      + [httpx.Response(200, json={'data': [{'embedding': [0.3]}]})])
    data = embed_providers._http_post_json(URL, {}, {'model': 'm', 'input': ['x']})
    assert data == {'data': [{'embedding': [0.3]}]}
    assert len(chiamate) == 9
    assert attese == [2.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
