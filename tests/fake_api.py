"""An in-memory AIWorkPay agent API, served through ``httpx.MockTransport``.

It follows the contract of Aiworkpay-v2: Bearer key, owner-only reads, scopes,
Idempotency-Key replay, stable error codes. ``fail`` queues answers to force
(an outage, a 429...), consumed one per request.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

ALL_SCOPES = ["missions:read", "missions:write", "missions:approve"]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeApi:
    def __init__(self) -> None:
        self.keys: Dict[str, Dict[str, Any]] = {
            "awp_agent_a_000000": {"label": "Agent A", "scopes": list(ALL_SCOPES), "max": 100, "monthly": 300},
            "awp_agent_b_000000": {"label": "Agent B", "scopes": ["missions:read"], "max": None, "monthly": None},
        }
        self.missions: Dict[str, Dict[str, Any]] = {}
        self.payments: List[Dict[str, Any]] = []
        self.files: Dict[str, Tuple[bytes, str]] = {}
        self.chunked: Dict[str, List[bytes]] = {}  # served without a Content-Length
        self.webhook: Optional[Dict[str, Any]] = None
        self.deliveries: List[Dict[str, Any]] = []
        self.requests: List[httpx.Request] = []
        self.fail: List[Union[Tuple[int, Any, Dict[str, str]], Exception]] = []
        self._seq = 0
        self.transport = httpx.MockTransport(self.handle)

    # ── test helpers ─────────────────────────────────────────────────────────

    def add_mission(self, key: str = "awp_agent_a_000000", **over: Any) -> Dict[str, Any]:
        self._seq += 1
        now = datetime.now(timezone.utc)
        mission = {
            "id": f"msn_{self._seq:04d}",
            "title": "Vérifier un commerce",
            "description": "Aller sur place vérifier.",
            "budget": "20",
            "currency": "EUR",
            "deadline": iso(now + timedelta(days=3)),
            "priority": "MEDIUM",
            "status": "PUBLISHED",
            "createdAt": iso(now),
            "updatedAt": iso(now),
            "revisionCount": 0,
            "resultNote": None,
            "resultData": None,
            "deliveredAt": None,
            "attachments": [],
            "_key": key,
            "_idem": None,
            "_fp": None,
        }
        mission.update(over)
        self.missions[mission["id"]] = mission
        if mission["status"] not in ("PAYMENT_PENDING", "CREATED"):
            self.payments.append(
                {
                    "id": f"txn_{len(self.payments) + 1}",
                    "missionId": mission["id"],
                    "key": key,
                    "amount": float(mission["budget"]),
                    "createdAt": iso(now),
                }
            )
        return mission

    def advance(self, mission_id: str, **patch: Any) -> None:
        self.missions[mission_id].update(patch)

    def sent(self, method: str, path_prefix: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.startswith(path_prefix)]

    # ── transport ────────────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            item = self.fail.pop(0)
            if isinstance(item, Exception):
                raise item
            status, body, headers = item
            return self._json(status, body, headers)

        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not key:
            return self._json(401, {"error": "Clé API manquante", "code": "UNAUTHORIZED"})
        if key not in self.keys:
            return self._json(403, {"error": "Clé API invalide, expirée ou révoquée", "code": "INVALID_API_KEY"})
        agent = self.keys[key]
        path, method = request.url.path, request.method
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None

        def need(scope: str) -> Optional[httpx.Response]:
            if scope in agent["scopes"]:
                return None
            return self._json(
                403,
                {
                    "error": f"Cette clé API n'a pas la permission « {scope} »",
                    "code": "INSUFFICIENT_SCOPE",
                    "details": {"requiredScope": scope, "keyScopes": agent["scopes"]},
                },
            )

        if path == "/api/account" and method == "GET":
            return need("missions:read") or self._account(key, agent)
        if path == "/api/transactions" and method == "GET":
            return need("missions:read") or self._transactions(key, params)
        if path == "/api/webhook":
            return self._webhook(method, body, need)
        if path == "/api/webhook/test" and method == "POST":
            if denied := need("missions:write"):
                return denied
            if not self.webhook:
                return self._json(409, {"error": "Aucun webhook configuré", "code": "WEBHOOK_NOT_CONFIGURED"})
            return self._json(202, {"eventId": "evt_ping"})

        proof = re.fullmatch(r"/api/missions/([^/]+)/proofs/([^/]+)", path)
        if proof and method == "GET":
            mission, file_id = self.missions.get(proof.group(1)), proof.group(2)
            if mission and mission["_key"] == key and file_id in self.chunked:
                return httpx.Response(
                    200, content=iter(self.chunked[file_id]), headers={"content-type": "application/pdf"}
                )
            if not mission or mission["_key"] != key or file_id not in self.files:
                return self._json(404, {"error": "Fichier introuvable", "code": "PROOF_NOT_FOUND"})
            content, content_type = self.files[file_id]
            return httpx.Response(200, content=content, headers={"content-type": content_type})

        if path == "/api/missions" and method == "POST":
            return need("missions:write") or self._create(key, agent, request, body)
        if path == "/api/missions" and method == "GET":
            return need("missions:read") or self._list(key, params)

        match = re.fullmatch(r"/api/missions/([^/]+)(?:/(approve|request-changes|cancel))?", path)
        if not match:
            return self._json(404, {"error": "Route inconnue", "code": "NOT_FOUND"})
        mission_id, action = match.groups()
        scope = {
            None: "missions:read",
            "approve": "missions:approve",
            "request-changes": "missions:approve",
            "cancel": "missions:write",
        }[action]
        if denied := need(scope):
            return denied
        mission = self.missions.get(mission_id)
        if not mission or mission["_key"] != key:
            return self._json(404, {"error": "Mission introuvable", "code": "MISSION_NOT_FOUND"})
        if action is None:
            return self._json(200, self._public(mission))
        return self._act(mission, action, body)

    # ── routes ───────────────────────────────────────────────────────────────

    def _create(self, key: str, agent: Dict[str, Any], request: httpx.Request, body: Dict[str, Any]) -> httpx.Response:
        idem = request.headers.get("idempotency-key")
        if idem and not re.fullmatch(r"[A-Za-z0-9_.:-]{8,255}", idem):
            return self._json(400, {"error": "En-tête Idempotency-Key invalide", "code": "INVALID_IDEMPOTENCY_KEY"})
        fingerprint = json.dumps(body, sort_keys=True)
        if idem:
            for mission in self.missions.values():
                if mission["_key"] == key and mission["_idem"] == idem:
                    if mission["_fp"] != fingerprint:
                        return self._json(
                            422, {"error": "Cette Idempotency-Key a déjà servi", "code": "IDEMPOTENCY_KEY_REUSED"}
                        )
                    return self._json(
                        200,
                        {"mission": self._public(mission), "payment": self._checkout(mission)},
                        {"Idempotent-Replayed": "true"},
                    )
        if agent["max"] is not None and body["budget"] > agent["max"]:
            return self._json(
                403,
                {
                    "error": "Le budget dépasse le plafond par mission",
                    "code": "BUDGET_LIMIT_EXCEEDED",
                    "details": {"maxMissionBudget": agent["max"]},
                },
            )
        mission = self.add_mission(
            key,
            title=body["title"],
            description=body["description"],
            budget=str(body["budget"]),
            currency=body["currency"],
            deadline=body["deadline"],
            priority=body["priority"],
            status="PAYMENT_PENDING",
            _idem=idem,
            _fp=fingerprint,
            _body=body,
        )
        return self._json(201, {"mission": self._public(mission), "payment": self._checkout(mission)})

    def _list(self, key: str, params: Dict[str, str]) -> httpx.Response:
        limit, skip = int(params.get("limit", 50)), int(params.get("skip", 0))
        rows = [
            m
            for m in self.missions.values()
            if m["_key"] == key and (not params.get("status") or m["status"] == params["status"])
        ]
        rows.sort(key=lambda m: m["id"], reverse=True)
        return self._json(200, [self._public(m) for m in rows[skip : skip + limit]])

    def _act(self, mission: Dict[str, Any], action: str, body: Optional[Dict[str, Any]]) -> httpx.Response:
        status = mission["status"]
        if action == "approve":
            if status == "COMPLETED":
                return self._json(
                    200,
                    {
                        "mission": self._public(mission),
                        "alreadyApproved": True,
                        "commission": {"payworkerAmount": 18, "platformFeeAmount": 2, "transferStatus": "SUCCEEDED"},
                    },
                )
            if status != "DELIVERED":
                return self._json(
                    409,
                    {
                        "error": f"Seule une mission livrée peut être validée (statut actuel : {status})",
                        "code": "MISSION_NOT_DELIVERED",
                        "details": {"status": status},
                    },
                )
            mission["status"] = "COMPLETED"
            return self._json(
                200,
                {
                    "mission": self._public(mission),
                    "alreadyApproved": False,
                    "commission": {
                        "payworkerAmount": 18,
                        "platformFeeAmount": 2,
                        "payworkerRate": "90%",
                        "platformRate": "10%",
                        "transferStatus": "SUCCEEDED",
                    },
                },
            )
        if action == "request-changes":
            if not body or len(body.get("feedback", "")) < 10:
                return self._json(400, {"error": "Données invalides", "code": "INVALID_BODY"})
            if status != "DELIVERED":
                return self._json(
                    409, {"error": "pas livrée", "code": "MISSION_NOT_DELIVERED", "details": {"status": status}}
                )
            if mission["revisionCount"] >= 3:
                return self._json(409, {"error": "limite", "code": "REVISION_LIMIT_REACHED"})
            mission.update(status="IN_PROGRESS", revisionCount=mission["revisionCount"] + 1)
            return self._json(200, {"mission": self._public(mission), "revisionsLeft": 3 - mission["revisionCount"]})
        if status == "CANCELED":
            return self._json(200, {"mission": self._public(mission), "payment": "already_canceled"})
        if status not in ("PAYMENT_PENDING", "PUBLISHED"):
            return self._json(
                409,
                {
                    "error": "Un Payworker est déjà engagé",
                    "code": "MISSION_NOT_CANCELABLE",
                    "details": {"status": status},
                },
            )
        refunded = status == "PUBLISHED"
        mission["status"] = "CANCELED"
        return self._json(
            200, {"mission": self._public(mission), "payment": "refunded" if refunded else "checkout_closed"}
        )

    def _account(self, key: str, agent: Dict[str, Any]) -> httpx.Response:
        mine = [m for m in self.missions.values() if m["_key"] == key]
        committed = sum(float(m["budget"]) for m in mine if m["status"] != "CANCELED")
        paid = [p for p in self.payments if p["key"] == key]
        refunded = sum(p["amount"] for p in paid if self.missions[p["missionId"]]["status"] == "CANCELED")
        total = sum(p["amount"] for p in paid)
        by_status: Dict[str, int] = {}
        for m in mine:
            by_status[m["status"]] = by_status.get(m["status"], 0) + 1
        return self._json(
            200,
            {
                "key": {
                    "id": "key_1",
                    "label": agent["label"],
                    "prefix": key[:12],
                    "scopes": agent["scopes"],
                    "active": True,
                    "expiresAt": None,
                    "lastUsedAt": None,
                    "createdAt": iso(datetime.now(timezone.utc)),
                },
                "limits": {"maxMissionBudget": agent["max"], "monthlyBudget": agent["monthly"]},
                "usage": {
                    "monthStart": iso(datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0)),
                    "committedThisMonth": committed,
                    "remainingThisMonth": None if agent["monthly"] is None else max(0, agent["monthly"] - committed),
                    "totalPaid": total,
                    "totalRefunded": refunded,
                    "netPaid": total - refunded,
                },
                "missions": {"total": len(mine), "byStatus": by_status},
                "webhook": {"configured": self.webhook is not None},
            },
        )

    def _transactions(self, key: str, params: Dict[str, str]) -> httpx.Response:
        limit, skip = int(params.get("limit", 50)), int(params.get("skip", 0))
        rows = [
            p
            for p in self.payments
            if p["key"] == key and (not params.get("missionId") or p["missionId"] == params["missionId"])
        ]
        rows.reverse()
        return self._json(
            200,
            {
                "total": len(rows),
                "transactions": [
                    {
                        "id": p["id"],
                        "type": "AGENT_PAYMENT",
                        "status": "SUCCEEDED",
                        "amount": p["amount"],
                        "currency": "EUR",
                        "missionId": p["missionId"],
                        "missionStatus": self.missions[p["missionId"]]["status"],
                        "refunded": self.missions[p["missionId"]]["status"] == "CANCELED",
                        "createdAt": p["createdAt"],
                    }
                    for p in rows[skip : skip + limit]
                ],
            },
        )

    def _webhook(self, method: str, body: Optional[Dict[str, Any]], need: Any) -> httpx.Response:
        if method == "GET":
            return need("missions:read") or self._json(
                200,
                {
                    "webhook": {"url": self.webhook["url"], "updatedAt": iso(datetime.now(timezone.utc))}
                    if self.webhook
                    else None,
                    "events": ["mission.published", "mission.delivered"],
                    "recentDeliveries": self.deliveries,
                },
            )
        if denied := need("missions:write"):
            return denied
        if method == "PUT":
            assert body is not None
            if not body["url"].startswith("https://"):
                return self._json(422, {"error": "L'URL doit utiliser https", "code": "INVALID_WEBHOOK_URL"})
            issue = body.get("rotateSecret") is True or not self.webhook
            self.webhook = {"url": body["url"]}
            answer: Dict[str, Any] = {
                "webhook": {"url": body["url"]},
                "events": ["mission.published", "mission.delivered"],
            }
            if issue:
                answer["secret"] = "awhsec_issued_once"
            return self._json(200, answer)
        self.webhook = None
        return self._json(200, {"webhook": None})

    # ── shapes ───────────────────────────────────────────────────────────────

    @staticmethod
    def _public(mission: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in mission.items() if not k.startswith("_")}

    @staticmethod
    def _checkout(mission: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "mode": "checkout",
            "checkoutSessionId": f"cs_{mission['id']}",
            "url": f"https://checkout.stripe.com/c/pay/cs_{mission['id']}",
            "expiresAt": iso(datetime.now(timezone.utc) + timedelta(days=1)),
        }

    @staticmethod
    def _json(status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {})
