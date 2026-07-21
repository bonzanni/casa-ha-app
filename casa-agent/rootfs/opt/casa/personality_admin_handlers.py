"""Personality Phase A, Task 14: Unix-socket-only admin routes.

Five ``POST`` routes registered ONLY on the internal Unix-socket
``AppRunner`` built by ``casa_core.start_internal_unix_runner`` (the
svc-casa-mcp socket) — NEVER on the public port-8099 app. They back
``casactl persona inspect/render/diff``, ``casactl specialist status``,
and ``casactl explain``.

Privacy: ``/admin/explain`` defaults to ``show_sensitive=False`` (strips
``system_prompt``/``memory_text`` via ``ExplanationStore.get``) and
requires ``confirmed=true`` in the request body whenever
``show_sensitive=true`` is requested (400 otherwise) — the interactive
TTY + typed ``SHOW`` gate lives in ``casactl`` itself, one layer up.
"""
from __future__ import annotations

from pathlib import Path

from aiohttp import web


def specialist_status_payload(runtime, *, slug: str) -> dict[str, object]:
    from specialist_registry import get_installed_instance

    instance = get_installed_instance(slug)
    if instance is None:
        return {"slug": slug, "state": "not_installed"}

    def _tuple_view(value):
        if value is None:
            return None
        return {
            "root": value.root,
            "persona_id": value.binding.persona_id,
            "persona_version": value.binding.persona_version,
            "binding_digest": value.binding.binding_digest,
            "dependency_digests": list(value.binding.dependency_digests),
            "effective_config_digest": value.binding.effective_config_digest,
            "config_digest": value.config_digest,
        }

    return {
        "slug": slug,
        "stable_agent_id": instance.stable_agent_id,
        "state": instance.state,
        "active": _tuple_view(instance.active),
        "desired": _tuple_view(instance.desired),
        "last_activation_error": instance.last_activation_error,
    }


def register_personality_admin_routes(
    app: "web.Application",
    *,
    runtime,
    persona_roots: tuple[Path, ...] = (Path("/config/personas"), Path("/opt/casa/defaults/personas")),
) -> None:
    async def _inspect(request: "web.Request") -> "web.Response":
        body = await request.json()
        ref = body.get("persona")
        if not isinstance(ref, str) or not ref:
            return web.json_response({"error": "invalid_persona_ref"}, status=400)
        pack = runtime.persona_packs.get(ref)
        if pack is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({
            "persona_id": pack.persona_id,
            "version": pack.version,
            "checksum": pack.checksum,
            "traits": dict(pack.traits),
            "sections": ["Core", "Negative space"],
        })

    async def _render(request: "web.Request") -> "web.Response":
        body = await request.json()
        role_id, projection = body.get("role"), body.get("projection")
        bundle = runtime.compiled_prompt_bundles.get(role_id)
        if bundle is None or projection not in {"text", "voice", "restricted_webhook"}:
            return web.json_response({"error": "not_found"}, status=404)
        selected = getattr(bundle, projection)
        return web.json_response({
            "digest": selected.digest,
            "estimated_tokens": selected.estimated_tokens,
            "system_prompt": selected.system_prompt,
        })

    async def _diff(request: "web.Request") -> "web.Response":
        body = await request.json()
        role_id, to_ref = body.get("role"), body.get("to")
        role = runtime.role_slots.get(role_id)
        target_persona = runtime.persona_packs.get(to_ref)
        if role is None or target_persona is None:
            return web.json_response({"error": "not_found"}, status=404)
        current_binding = runtime.bindings.get(role_id)
        return web.json_response({
            "role": role_id,
            "current_persona": current_binding.persona_id if current_binding else None,
            "target_persona": target_persona.persona_id,
            "target_checksum": target_persona.checksum,
        })

    async def _specialist_status(request: "web.Request") -> "web.Response":
        body = await request.json()
        slug = body.get("slug")
        if not isinstance(slug, str) or not slug:
            return web.json_response({"error": "invalid_slug"}, status=400)
        return web.json_response(specialist_status_payload(runtime, slug=slug))

    async def _explain(request: "web.Request") -> "web.Response":
        body = await request.json()
        cid = body.get("correlation_id")
        show_sensitive = bool(body.get("show_sensitive"))
        confirmed = bool(body.get("confirmed"))
        if show_sensitive and not confirmed:
            return web.json_response({"error": "confirmation_required"}, status=400)
        if not isinstance(cid, str) or not cid:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            return web.json_response(runtime.explanation_store.get(cid, show_sensitive=show_sensitive))
        except (KeyError, ValueError):
            return web.json_response({"error": "not_found"}, status=404)

    app.router.add_post("/admin/personality/inspect", _inspect)
    app.router.add_post("/admin/personality/render", _render)
    app.router.add_post("/admin/personality/diff", _diff)
    app.router.add_post("/admin/specialist/status", _specialist_status)
    app.router.add_post("/admin/explain", _explain)
