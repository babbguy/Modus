#!/usr/bin/env python3
"""
Seed the Modus database with realistic demo data for every dashboard view.
Run: python scripts/seed_demo_data.py
"""
import hashlib
import json
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DB_PATH = "modus.db"
NOW = datetime.now(timezone.utc)

def _uuid():
    return str(uuid.uuid4())

def _iso(dt):
    # Same text form SQLAlchemy writes for DateTime columns on SQLite (UTC,
    # space separator); raw-SQL date comparisons in the API rely on it.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

def _hash(s):
    return hashlib.sha256(s.encode()).hexdigest()

def _safe_insert(c, sql, params):
    """Try insert, skip on constraint errors."""
    try:
        c.execute(sql, params)
    except sqlite3.IntegrityError:
        pass

def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    providers = ["anthropic", "openai", "google", "mistral"]
    models_map = {
        "anthropic": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001", "claude-opus-4-20250115"],
        "openai": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
        "google": ["gemini-2.0-flash", "gemini-2.5-pro"],
        "mistral": ["mistral-large-latest", "codestral-latest"],
    }

    # ── TEAMS ──────────────────────────────────────────────────────
    teams = [
        (_uuid(), "platform-eng", "Platform Engineering", "Engineering", 50000),
        (_uuid(), "ml-research", "ML Research", "Research", 80000),
        (_uuid(), "product-api", "Product API", "Product", 25000),
        (_uuid(), "data-science", "Data Science", "Analytics", 35000),
    ]
    for tid, slug, name, dept, budget in teams:
        _safe_insert(c, """INSERT INTO teams (id, slug, name, department, budget_monthly_usd, current_spend_usd, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, slug, name, dept, budget, round(budget * random.uniform(0.3, 0.85), 2),
             _iso(NOW - timedelta(days=90)), _iso(NOW)))
    conn.commit()
    print("  teams OK")

    # ── COST CENTERS ───────────────────────────────────────────────
    cc_ids = []
    for cc_name, code, dept in [("Engineering", "ENG-001", "Engineering"), ("Research", "RES-001", "Research"),
                                 ("Product", "PRD-001", "Product"), ("Analytics", "ANA-001", "Analytics")]:
        ccid = _uuid()
        cc_ids.append(ccid)
        _safe_insert(c, """INSERT INTO cost_centers (id, name, code, department, budget_monthly_usd, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (ccid, cc_name, code, dept, random.randint(20000, 80000), _iso(NOW - timedelta(days=60)), _iso(NOW)))
    conn.commit()
    print("  cost_centers OK")

    # ── APPS ───────────────────────────────────────────────────────
    apps = []
    app_defs = [
        ("chatbot-api", "Chatbot API", teams[0][0], "production", "1.8.2"),
        ("code-review", "Code Review Agent", teams[0][0], "production", "2.1.0"),
        ("search-engine", "Semantic Search", teams[1][0], "production", "1.5.7"),
        ("experiment-runner", "Experiment Runner", teams[1][0], "staging", "0.9.3"),
        ("customer-support", "Customer Support Bot", teams[2][0], "production", "3.0.1"),
        ("content-gen", "Content Generator", teams[2][0], "production", "1.2.4"),
        ("analytics-agent", "Analytics Agent", teams[3][0], "production", "1.0.0"),
        ("data-pipeline", "Data Pipeline", teams[3][0], "development", "0.5.0"),
    ]
    for app_id_slug, app_name, team_id, env, ver in app_defs:
        aid = _uuid()
        key = f"mds_{uuid.uuid4().hex[:24]}"
        apps.append((aid, app_id_slug, team_id))
        _safe_insert(c, """INSERT INTO apps
            (id, team_id, app_id, app_name, environment, api_key_hash, api_key_prefix, agent_version,
             last_seen_at, first_seen_at, is_active, enforcement_state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)""",
            (aid, team_id, app_id_slug, app_name, env, _hash(key), key[:12], ver,
             _iso(NOW - timedelta(minutes=random.randint(0, 4))),
             _iso(NOW - timedelta(days=random.randint(30, 90))),
             _iso(NOW - timedelta(days=90)), _iso(NOW)))
    conn.commit()
    print("  apps OK")

    # ── USAGE AGGREGATES (daily, 30 days) ──────────────────────────
    print("  seeding usage_aggregates (30 days)...")
    for day_offset in range(30):
        dt = NOW - timedelta(days=day_offset)
        ps = dt.replace(hour=0, minute=0, second=0)
        pe = ps + timedelta(days=1) - timedelta(seconds=1)
        for aid, app_slug, team_id in apps:
            provider = random.choice(providers)
            model = random.choice(models_map[provider])
            calls = random.randint(50, 2000)
            inp_tok = calls * random.randint(200, 2000)
            out_tok = calls * random.randint(50, 800)
            inp_cost = round(inp_tok * 0.000003, 8)
            out_cost = round(out_tok * 0.000015, 8)
            total_cost = round(inp_cost + out_cost, 8)
            avg_dur = random.randint(200, 1500)
            _safe_insert(c, """INSERT INTO usage_aggregates
                (id, app_id, team_id, provider, model, resource_type, granularity,
                 period_start, period_end, call_count, input_tokens, output_tokens, total_tokens,
                 input_cost, output_cost, total_cost,
                 avg_duration_ms, min_duration_ms, max_duration_ms, duration_ms_sum, p95_duration_ms,
                 source, computed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_uuid(), aid, team_id, provider, model, "llm", "daily",
                 _iso(ps), _iso(pe), calls, inp_tok, out_tok, inp_tok + out_tok,
                 inp_cost, out_cost, total_cost,
                 avg_dur, avg_dur - 100, avg_dur + 800, avg_dur * calls, avg_dur + 400,
                 "sdk", _iso(dt)))
    conn.commit()
    print("  usage_aggregates OK")

    # ── USAGE RECORDS (last 2 days) ────────────────────────────────
    print("  seeding usage_records (2 days)...")
    for hour_offset in range(48):
        dt = NOW - timedelta(hours=hour_offset)
        for aid, app_slug, team_id in apps[:4]:
            provider = random.choice(providers)
            model = random.choice(models_map[provider])
            for _ in range(random.randint(2, 10)):
                inp = random.randint(100, 5000)
                out = random.randint(20, 2000)
                inp_c = round(inp * 0.000003, 8)
                out_c = round(out * 0.000015, 8)
                ts = dt + timedelta(minutes=random.randint(0, 59))
                _safe_insert(c, """INSERT INTO usage_records
                    (id, app_id, team_id, provider, resource_type, model, operation,
                     input_tokens, output_tokens, total_tokens, input_cost, output_cost, total_cost,
                     duration_ms, timestamp, ingested_at, session_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_uuid(), aid, team_id, provider, "llm", model, "chat",
                     inp, out, inp + out, inp_c, out_c, round(inp_c + out_c, 8),
                     random.randint(150, 3000),
                     _iso(ts), _iso(ts), _uuid()))
    conn.commit()
    print("  usage_records OK")

    # ── THRESHOLDS ─────────────────────────────────────────────────
    tid0 = teams[0][0]
    for name, scope, metric, period, warn, crit in [
        ("Daily Cost Alert", "global", "cost", "daily", 500, 1000),
        ("Hourly Token Spike", "global", "tokens", "hourly", 500000, 1000000),
        ("API Call Rate", "global", "calls", "hourly", 5000, 10000),
        ("Latency P99", "global", "latency_p99", "hourly", 3000, 5000),
        ("Error Rate", "global", "error_rate", "daily", 2, 5),
        ("Team Budget", "team", "cost", "monthly", 20000, 40000),
    ]:
        _safe_insert(c, """INSERT INTO thresholds
            (id, team_id, name, scope, metric, period, warning_value, critical_value, is_active, degradation_enabled, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,1,0,?,?)""",
            (_uuid(), tid0, name, scope, metric, period, warn, crit,
             _iso(NOW - timedelta(days=30)), _iso(NOW)))
    conn.commit()
    print("  thresholds OK")

    # ── ALERTS ─────────────────────────────────────────────────────
    for i in range(25):
        sev = random.choice(["critical", "warning", "info"])
        metric = random.choice(["cost", "tokens", "calls", "latency_p99", "error_rate"])
        fired = NOW - timedelta(hours=random.randint(1, 720))
        aid, app_slug, team_id = random.choice(apps)
        thresh = random.uniform(100, 5000)
        actual = thresh * random.uniform(1.1, 3.0)
        resolved = fired + timedelta(hours=random.randint(1, 12)) if random.random() > 0.3 else None
        _safe_insert(c, """INSERT INTO alerts
            (id, threshold_id, app_id, team_id, severity, metric, threshold_value, actual_value,
             period_start, period_end, fired_at, acknowledged_at, notification_sent)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (_uuid(), _uuid(), aid, team_id, sev, metric, round(thresh, 2), round(actual, 2),
             _iso(fired - timedelta(hours=1)), _iso(fired),
             _iso(fired), _iso(resolved) if resolved else None))
    conn.commit()
    print("  alerts OK")

    # ── POLICIES ───────────────────────────────────────────────────
    policy_names = []
    for name, ptype, effect, scope, priority, config in [
        ("Global Daily Budget Cap", "budget_cap", "deny", "global", 1,
         json.dumps({"limit": 5000, "window": "daily"})),
        ("Rate Limit Production", "rate_limit", "deny", "global", 2,
         json.dumps({"max_calls": 1000, "per_seconds": 60})),
        ("Token Cap per Call", "token_cap", "warn", "global", 3,
         json.dumps({"max_tokens": 100000})),
        ("Block GPT-4 in Staging", "model_denylist", "deny", "app", 4,
         json.dumps({"models": ["gpt-4", "gpt-4-turbo"]})),
        ("Require Justification", "require_justification", "warn", "global", 5,
         json.dumps({"min_length": 20})),
        ("Cost per Call Limit", "cost_per_call", "deny", "global", 6,
         json.dumps({"max_cost": 5.0})),
    ]:
        policy_names.append(name)
        _safe_insert(c, """INSERT INTO governance_policies
            (id, name, description, policy_type, effect, scope, priority, is_active, config, created_by, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,1,?,?,?,?)""",
            (_uuid(), name, f"Auto-generated {ptype} policy", ptype, effect, scope, priority, config,
             "admin@example.com", _iso(NOW - timedelta(days=45)), _iso(NOW)))
    conn.commit()
    print("  governance_policies OK")

    # ── POLICY DECISIONS ───────────────────────────────────────────
    print("  seeding policy_decisions...")
    decisions = ["allow", "deny", "warn", "throttle", "redirect"]
    for i in range(200):
        dt = NOW - timedelta(hours=random.randint(0, 720))
        aid, _, team_id = random.choice(apps)
        decision = random.choices(decisions, weights=[60, 10, 15, 10, 5])[0]
        prov = random.choice(providers)
        _safe_insert(c, """INSERT INTO policy_decisions
            (id, app_id, team_id, decision, reason, request_provider, request_model,
             request_estimated_tokens, request_estimated_cost,
             evaluation_latency_ms, decided_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), aid, team_id, decision,
             f"Policy '{random.choice(policy_names)}' evaluated",
             prov, random.choice(models_map[prov]),
             random.randint(100, 10000), round(random.uniform(0.001, 2.0), 6),
             random.randint(1, 10), _iso(dt)))
    conn.commit()
    print("  policy_decisions OK")

    # ── PRICING OVERRIDES ──────────────────────────────────────────
    for prov, model, inp, out, reason in [
        ("openai", "gpt-4o", 0.005, 0.015, "Negotiated enterprise rate"),
        ("anthropic", "claude-sonnet-4-20250514", 0.003, 0.015, "Volume discount"),
        ("google", "gemini-2.5-pro", 0.00125, 0.005, "Free tier credit"),
    ]:
        _safe_insert(c, """INSERT INTO pricing_overrides
            (id, provider, model, resource_type, input_cost_per_1k, output_cost_per_1k, override_reason, is_active, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,1,?,?)""",
            (_uuid(), prov, model, "llm", inp, out, reason, _iso(NOW - timedelta(days=15)), _iso(NOW)))
    conn.commit()
    print("  pricing_overrides OK")

    # ── AUDIT LOG ──────────────────────────────────────────────────
    for i in range(40):
        dt = NOW - timedelta(hours=random.randint(0, 720))
        action = random.choice(["app.registered", "app.key_rotated", "policy.created", "policy.updated",
                                 "threshold.created", "team.created", "user.invited", "settings.updated",
                                 "nomus.synced"])
        _safe_insert(c, """INSERT INTO audit_log
            (id, actor_id, actor_ip, team_id, resource_type, resource_id, action, occurred_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (_uuid(), "admin@example.com", f"10.0.{random.randint(1,255)}.{random.randint(1,255)}",
             random.choice(teams)[0], action.split(".")[0], _uuid(), action, _iso(dt)))
    conn.commit()
    print("  audit_log OK")

    # ── USERS ──────────────────────────────────────────────────────
    for uname, email, active in [
        ("Admin User", "admin@example.com", True),
        ("Sarah Chen", "sarah@acme.com", True),
        ("James Rodriguez", "james@acme.com", True),
        ("Emily Nakamura", "emily@acme.com", True),
        ("Pending Invite", "invite@acme.com", False),
    ]:
        _safe_insert(c, """INSERT INTO users
            (id, email, display_name, is_active, last_login_at, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)""",
            (_uuid(), email, uname, active,
             _iso(NOW - timedelta(minutes=random.randint(5, 1440))),
             _iso(NOW - timedelta(days=60)), _iso(NOW)))
    conn.commit()
    print("  users OK")

    # ── ROUTING FINGERPRINTS ───────────────────────────────────────
    phases = ["routing", "routing", "routing", "observe", "calibrating", "excluded"]
    for i in range(30):
        aid, app_slug, _ = random.choice(apps)
        fp_hash = hashlib.sha256(f"fp_{i}_{app_slug}".encode()).hexdigest()[:16]
        phase = random.choice(phases)
        conf = round(random.uniform(0.6, 0.99), 3) if phase == "routing" else round(random.uniform(0.2, 0.7), 3)
        _safe_insert(c, """INSERT INTO routing_fingerprints
            (id, app_id, fingerprint_hash, system_prompt_hash, call_site_id, phase, routing_confidence, drift_score,
             total_routed_calls, total_escalations, cheap_model_agreement_rate,
             allow_routing, cheap_model, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), aid, fp_hash, _hash(f"sp_{i}"), f"module.{app_slug}.handler_{i}",
             phase, conf, round(random.uniform(0, 0.3), 3),
             random.randint(100, 50000), random.randint(0, 500),
             round(random.uniform(0.7, 0.98), 3),
             phase != "excluded", "claude-haiku-4-5-20251001",
             _iso(NOW - timedelta(days=20)), _iso(NOW)))
    conn.commit()
    print("  routing_fingerprints OK")

    # ── GOVERNANCE PROPOSALS ───────────────────────────────────────
    for i in range(15):
        dt = NOW - timedelta(hours=random.randint(1, 500))
        tid = random.choice(teams)[0]
        _safe_insert(c, """INSERT INTO governance_proposals
            (id, team_id, proposal_type, severity, title, rationale, source, status,
             estimated_savings_usd, current_yaml, proposed_yaml, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), tid,
             random.choice(["budget_cap", "rate_limit", "model_swap", "degradation_ladder"]),
             random.choice(["low", "medium", "high", "critical"]),
             f"Auto: {random.choice(['Reduce', 'Optimize', 'Cap', 'Route'])} {random.choice(['spend', 'tokens', 'calls'])} for {random.choice([a[1] for a in apps])}",
             "Autonomous governance detected an optimization opportunity based on recent usage patterns.",
             random.choice(["anomaly_detector", "governance_loop", "budget_monitor", "seasonal_pattern"]),
             random.choice(["pending", "pending", "applied", "dismissed"]),
             round(random.uniform(50, 5000), 2),
             "budget_cap:\n  limit: 5000\n  window: daily",
             "budget_cap:\n  limit: 3000\n  window: daily",
             _iso(dt)))
    conn.commit()
    print("  governance_proposals OK")

    # ── REWIND EVENTS ──────────────────────────────────────────────
    for i in range(20):
        dt = NOW - timedelta(hours=random.randint(1, 300))
        aid, _, team_id = random.choice(apps)
        _safe_insert(c, """INSERT INTO rewind_events
            (id, session_id, app_id, team_id, trigger_reason, actions_rolled_back,
             failure_context, policy_diff_generated, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (_uuid(), _uuid(), aid, team_id,
             random.choice(["Budget exceeded", "Token cap hit", "Rate limit triggered", "Model not allowed"]),
             json.dumps([
                 {"tool_name": random.choice(["send_email", "write_file", "http_post"]), "success": True, "error": None}
                 for _ in range(random.randint(1, 5))
             ]),
             json.dumps({"model": random.choice(models_map[random.choice(providers)]), "tokens": random.randint(1000, 50000)}),
             random.choice([True, False]),
             _iso(dt)))
    conn.commit()
    print("  rewind_events OK")

    # ── COT LEDGER ENTRIES ─────────────────────────────────────────
    prev_hash = _hash("genesis")
    for i in range(30):
        dt = NOW - timedelta(hours=random.randint(1, 500))
        tid = random.choice(teams)[0]
        entry_hash = _hash(f"cot_{i}_{prev_hash}")
        _safe_insert(c, """INSERT INTO cot_ledger_entries
            (id, team_id, seq_num, prev_hash, entry_hash, decision_type, trigger,
             decision_summary, evidence_snapshot, reasoning_steps, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), tid, i + 1, prev_hash, entry_hash,
             random.choice(["proposal_applied", "proposal_dismissed", "policy_evolved",
                            "alert_generated", "rewind_triggered", "budget_adjusted"]),
             f"Triggered by {random.choice(['anomaly_detector', 'governance_loop', 'budget_monitor'])}",
             f"Decision: {random.choice(['Apply budget cap reduction', 'Dismiss model swap proposal', 'Evolve rate limit policy', 'Alert on cost spike'])}",
             json.dumps({"metrics": {"cost_30d": round(random.uniform(100, 5000), 2)}}),
             json.dumps([f"Step {j}: Evaluated {random.choice(['cost', 'risk', 'savings'])}" for j in range(1, 4)]),
             _iso(dt)))
        prev_hash = entry_hash
    conn.commit()
    print("  cot_ledger_entries OK")

    # ── ANOMALY EVENTS ─────────────────────────────────────────────
    for i in range(15):
        dt = NOW - timedelta(hours=random.randint(1, 168))
        aid, app_slug, team_id = random.choice(apps)
        _safe_insert(c, """INSERT INTO anomaly_events
            (id, app_id, team_id, metric, severity, z_score,
             baseline_value, actual_value, window_hours,
             ai_explanation, detected_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), aid, team_id,
             random.choice(["cost", "tokens", "latency", "error_rate"]),
             random.choice(["critical", "warning", "info"]),
             round(random.uniform(2.0, 6.0), 2),
             round(random.uniform(100, 3000), 2),
             round(random.uniform(500, 10000), 2),
             random.choice([6, 12, 24]),
             f"Detected unusual pattern in {app_slug}. Current value is {round(random.uniform(2, 5), 1)}x above the 7-day baseline.",
             _iso(dt)))
    conn.commit()
    print("  anomaly_events OK")

    # ── OPTIMIZATION RECOMMENDATIONS ───────────────────────────────
    for i in range(8):
        aid, app_slug, team_id = random.choice(apps)
        from_model = random.choice(["claude-sonnet-4-20250514", "gpt-4o", "gemini-2.5-pro"])
        to_model = random.choice(["claude-haiku-4-5-20251001", "gpt-4o-mini", "gemini-2.0-flash"])
        prov = random.choice(providers)
        _safe_insert(c, """INSERT INTO optimization_recommendations
            (id, app_id, team_id, current_model, suggested_model, provider,
             call_volume_basis, estimated_monthly_savings, confidence,
             recommendation_text, generated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), aid, team_id, from_model, to_model, prov,
             random.randint(1000, 50000),
             round(random.uniform(100, 3000), 2),
             round(random.uniform(0.85, 0.98), 3),
             f"Switch {app_slug} from {from_model} to {to_model}. Quality analysis shows 94%+ agreement.",
             _iso(NOW - timedelta(days=random.randint(1, 14)))))
    conn.commit()
    print("  optimization_recommendations OK")

    # ── SPEND FORECASTS ────────────────────────────────────────────
    for tid, slug, name, dept, budget in teams:
        _safe_insert(c, """INSERT INTO spend_forecasts
            (id, team_id, period_label, mtd_actual, forecast_eom, forecast_eoq, forecast_eoy,
             trend_pct, r_squared, basis_days, computed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), tid, NOW.strftime("%Y-%m"),
             round(budget * random.uniform(0.3, 0.7), 2),
             round(budget * random.uniform(0.85, 1.15), 2),
             round(budget * 3 * random.uniform(0.9, 1.1), 2),
             round(budget * 12 * random.uniform(0.9, 1.1), 2),
             round(random.uniform(-5, 15), 2),
             round(random.uniform(0.82, 0.97), 4),
             21, _iso(NOW)))
    conn.commit()
    print("  spend_forecasts OK")

    # ── ENFORCEMENT ATTESTATIONS ───────────────────────────────────
    for i in range(50):
        dt = NOW - timedelta(hours=random.randint(0, 720))
        aid, _, team_id = random.choice(apps)
        nonce = uuid.uuid4().hex[:16]
        _safe_insert(c, """INSERT INTO enforcement_attestations
            (id, decision_id, attestation_hash, signature, nonce, algorithm, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (_uuid(), _uuid(), _hash(f"att_{i}_{dt}"),
             _hash(f"sig_{i}_{nonce}"), nonce, "RS256",
             json.dumps({"decision": random.choice(["allow", "deny", "warn"]),
                         "policy": random.choice(policy_names), "app": aid}),
             _iso(dt)))
    conn.commit()
    print("  enforcement_attestations OK")

    # ── TRISM THREATS ──────────────────────────────────────────────
    threat_types = ["prompt_injection", "data_exfiltration", "model_abuse", "jailbreak_attempt",
                    "adversarial_input", "credential_leak", "pii_exposure"]
    for i in range(20):
        dt = NOW - timedelta(hours=random.randint(1, 336))
        aid, app_slug, team_id = random.choice(apps)
        sev = random.choices(["critical", "high", "medium", "low"], weights=[5, 15, 40, 40])[0]
        tt = random.choice(threat_types)
        _safe_insert(c, """INSERT INTO trism_threat_events
            (id, session_id, app_id, team_id, threat_type, severity,
             confidence_score, detection_method, indicators_json,
             action_taken, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), _uuid(), aid, team_id, tt, sev,
             round(random.uniform(0.6, 0.99), 3),
             random.choice(["pattern_match", "ml_classifier", "heuristic"]),
             json.dumps({"pattern_id": f"P-{random.randint(100,999)}", "tokens_scanned": random.randint(500, 5000)}),
             "blocked" if sev in ("critical", "high") else "flagged",
             _iso(dt)))
    conn.commit()
    print("  trism_threat_events OK")

    # ── ZK TRAJECTORY PROOFS ───────────────────────────────────────
    for i in range(30):
        dt = NOW - timedelta(hours=random.randint(0, 336))
        _safe_insert(c, """INSERT INTO trajectory_proofs
            (id, session_id, app_id, team_id, proof_type, proof_status,
             proof_data, verification_key_hash, prover_time_ms, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), _uuid(), random.choice(apps)[0], random.choice(teams)[0],
             random.choice(["hash_chain", "snark"]),
             random.choice(["verified", "verified", "verified", "pending"]),
             _hash(f"proof_{i}"), _hash(f"vk_{i}"),
             random.randint(1, 50), _iso(dt)))
    conn.commit()
    print("  trajectory_proofs OK")

    # ── PQC READINESS SCORES ──────────────────────────────────────
    for tid, slug, _, _, _ in teams:
        aid = random.choice([a[0] for a in apps if a[2] == tid])
        _safe_insert(c, """INSERT INTO pqc_readiness_scores
            (id, team_id, app_id, score, classical_key_count, hybrid_key_count, pqc_key_count,
             weakest_algorithm, recommendations, assessed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), tid, aid, round(random.uniform(55, 85), 1),
             random.randint(5, 15), random.randint(1, 5), random.randint(0, 3),
             "RSA-2048", json.dumps(["Migrate TLS to ML-KEM", "Enable hybrid signatures"]),
             _iso(NOW)))
    conn.commit()
    print("  pqc_readiness_scores OK")

    # ── CHARGEBACK INVOICES ────────────────────────────────────────
    for month_offset in range(3):
        period_start = (NOW.replace(day=1) - timedelta(days=30 * month_offset)).replace(day=1)
        period_end = (period_start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
        ccid = random.choice(cc_ids) if cc_ids else None
        _safe_insert(c, """INSERT INTO chargeback_invoices
            (id, cost_center_id, period_start, period_end, total_cost_usd,
             line_items, format, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (_uuid(), ccid, _iso(period_start), _iso(period_end),
             round(random.uniform(15000, 60000), 2),
             json.dumps([{"team": t[1], "cost": round(random.uniform(2000, 15000), 2)} for t in teams]),
             "json", random.choice(["final", "final", "draft"]),
             _iso(period_start + timedelta(days=1))))
    conn.commit()
    print("  chargeback_invoices OK")

    # ── FINANCE REPORTS ────────────────────────────────────────────
    for rtype, rname in [("chargeback", "Monthly Chargeback"), ("forecast", "Weekly Forecast"),
                          ("anomaly", "Anomaly Digest"), ("executive_summary", "Executive Summary")]:
        _safe_insert(c, """INSERT INTO finance_reports
            (id, name, report_type, schedule, delivery_channel, delivery_target,
             format, is_active, last_run_at, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,1,?,?,?)""",
            (_uuid(), rname, rtype,
             random.choice(["daily", "weekly", "monthly"]),
             "email", "admin@example.com", "pdf",
             _iso(NOW - timedelta(days=1)),
             _iso(NOW - timedelta(days=30)), _iso(NOW)))
    conn.commit()
    print("  finance_reports OK")

    # ── SYSTEM SETTINGS ────────────────────────────────────────────
    for key, val in [("routing.enabled", "true"), ("routing.cheap_model", "claude-haiku-4-5-20251001"),
                      ("routing.observe_threshold", "200"), ("routing.max_misroute_rate", "0.05"),
                      ("routing.calibrator_interval_seconds", "600"),
                      ("routing.drift_monitor_interval_seconds", "300"),
                      ("routing.confidence_decay_rate", "0.85")]:
        c.execute("INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES (?,?,?)",
                  (key, val, _iso(NOW)))
    conn.commit()
    print("  system_settings OK")

    # ── FEDERATION PEERS ───────────────────────────────────────────
    for i, (pname, url) in enumerate([
        ("US-East Orchestrator", "https://us-east.modus.acme.com"),
        ("EU-West Orchestrator", "https://eu-west.modus.acme.com"),
        ("APAC Orchestrator", "https://apac.modus.acme.com"),
    ]):
        tid = random.choice(teams)[0]
        _safe_insert(c, """INSERT INTO federation_peers
            (id, team_id, name, peer_url, status, api_key_hash, api_key_prefix,
             last_heartbeat_at, last_heartbeat_latency_ms, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), tid, pname, url,
             random.choice(["connected", "connected", "error"]),
             _hash(f"fed_key_{i}"), f"mds_fed_{i}",
             _iso(NOW - timedelta(minutes=random.randint(1, 30))),
             random.randint(20, 300),
             _iso(NOW - timedelta(days=30)), _iso(NOW)))
    conn.commit()
    print("  federation_peers OK")

    # ── EVOLUTION GENERATIONS ──────────────────────────────────────
    tid = teams[0][0]
    for gen in range(1, 6):
        _safe_insert(c, """INSERT INTO evolution_generations
            (id, team_id, generation_number, population_size, best_fitness, avg_fitness,
             best_genome_yaml, mutations_applied, elapsed_ms, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), tid, gen, 100,
             round(0.5 + gen * 0.08 + random.uniform(-0.02, 0.02), 4),
             round(0.4 + gen * 0.06, 4),
             f"budget_cap:\n  limit: {5000 - gen * 200}\n  window: daily",
             random.randint(5, 25),
             random.randint(500, 5000),
             _iso(NOW - timedelta(hours=gen * 24))))
    conn.commit()
    print("  evolution_generations OK")

    # ── AGENT HEARTBEATS ───────────────────────────────────────────
    for aid, app_slug, team_id in apps:
        _safe_insert(c, """INSERT INTO agent_heartbeats
            (id, app_id, team_id, agent_version, instrumented_providers, received_at)
            VALUES (?,?,?,?,?,?)""",
            (_uuid(), aid, team_id, f"1.{random.randint(0,9)}.{random.randint(0,9)}",
             json.dumps(random.sample(providers, random.randint(1, 3))),
             _iso(NOW - timedelta(minutes=random.randint(0, 3)))))
    conn.commit()
    print("  agent_heartbeats OK")

    # ── REAL-TIME SPEND ────────────────────────────────────────────
    for aid, app_slug, team_id in apps:
        ws = NOW.replace(minute=0, second=0, microsecond=0)
        _safe_insert(c, """INSERT INTO real_time_spend
            (id, app_id, team_id, period, window_key, window_start, window_end,
             total_cost, call_count, input_tokens, output_tokens, total_duration_ms,
             hierarchy_level, hierarchy_key, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_uuid(), aid, team_id, "hourly", f"{app_slug}_{_iso(ws)}",
             _iso(ws), _iso(ws + timedelta(hours=1)),
             round(random.uniform(0.5, 50), 4),
             random.randint(10, 500),
             random.randint(5000, 500000),
             random.randint(1000, 100000),
             random.randint(50000, 500000),
             "app", app_slug, _iso(NOW)))
    conn.commit()
    print("  real_time_spend OK")

    conn.close()
    print("\nDone! All demo data seeded successfully.")
    print("Refresh the dashboard at http://localhost:8080 to see the data.")


if __name__ == "__main__":
    main()
