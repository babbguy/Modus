# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Baseline schema for PostgreSQL.

Revision ID: 0001
Revises:
Create Date: 2026-10-07 20:49:00.544535

Creates the full schema defined in orchestrator/db/models.py. SQLite
deployments do not use Alembic: init_db() creates tables with create_all.

usage_records is created as a plain table. Monthly range partitioning
(MODUS_PARTITION_USAGE_RECORDS) is opt-in and requires converting the table
to a partitioned table with a primary key of (id, timestamp) first.
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('attribution_nodes',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('call_id', sa.String(length=32), nullable=False),
    sa.Column('parent_call_id', sa.String(length=32), nullable=True),
    sa.Column('node_label', sa.String(length=256), nullable=False, comment='Human label: provider/model or span name.'),
    sa.Column('provider', sa.String(length=64), nullable=True),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('direct_cost', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False, comment="Cost of this node's own LLM calls."),
    sa.Column('attributed_cost', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False, comment='Tree Shapley attributed cost (includes downstream share).'),
    sa.Column('amplification_factor', sa.Float(), nullable=True, comment='downstream_cost / direct_cost. High = optimization target.'),
    sa.Column('retry_tax', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False),
    sa.Column('defensive_spend', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True, comment='0.0-1.0 per-node attribution confidence.'),
    sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('call_count', sa.Integer(), server_default='1', nullable=False),
    sa.Column('is_retry', sa.Boolean(), nullable=False),
    sa.Column('is_defensive', sa.Boolean(), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False, comment='Denormalized for query perf.'),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False, comment='Denormalized for query perf.'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_attribution_nodes_session_id'), 'attribution_nodes', ['session_id'], unique=False)
    op.create_index('ix_attrnode_app_amp', 'attribution_nodes', ['app_id', 'amplification_factor'], unique=False)
    op.create_index('ix_attrnode_session', 'attribution_nodes', ['session_id', 'call_id'], unique=False)
    op.create_table('audit_checkpoints',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('chain_seq', sa.BigInteger(), nullable=False),
    sa.Column('entry_hash', sa.String(length=64), nullable=False),
    sa.Column('signature', sa.String(length=256), nullable=True),
    sa.Column('public_key_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_checkpoint_seq', 'audit_checkpoints', ['chain_seq'], unique=False)
    op.create_table('audit_log',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('actor_id', sa.String(length=256), nullable=False),
    sa.Column('actor_ip', sa.String(length=64), nullable=True),
    sa.Column('team_id', sa.String(length=36), nullable=True),
    sa.Column('resource_type', sa.String(length=64), nullable=False),
    sa.Column('resource_id', sa.String(length=36), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('chain_seq', sa.BigInteger(), nullable=True),
    sa.Column('prev_hash', sa.String(length=64), nullable=True),
    sa.Column('entry_hash', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_actor', 'audit_log', ['actor_id', 'occurred_at'], unique=False)
    op.create_index(op.f('ix_audit_log_entry_hash'), 'audit_log', ['entry_hash'], unique=False)
    op.create_index('ix_audit_resource', 'audit_log', ['resource_type', 'resource_id', 'occurred_at'], unique=False)
    op.create_index('ix_audit_team', 'audit_log', ['team_id', 'occurred_at'], unique=False)
    op.create_table('billing_actuals',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False, comment='Cloud/AI provider: aws, gcp, azure, openai, anthropic'),
    sa.Column('service', sa.String(length=128), nullable=True, comment="Specific service (e.g. 'Amazon Bedrock', 'OpenAI API')"),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actual_cost_usd', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('inferred_cost_usd', sa.Numeric(precision=18, scale=8), nullable=True, comment="Modus's inferred cost for the same period — populated during reconciliation"),
    sa.Column('delta_usd', sa.Numeric(precision=18, scale=8), nullable=True, comment='actual - inferred. Positive = Modus under-counted.'),
    sa.Column('delta_pct', sa.Float(), nullable=True, comment='(actual - inferred) / actual * 100'),
    sa.Column('raw_data', sa.JSON(), nullable=True, comment='Original invoice line item data from the provider'),
    sa.Column('reconciled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_billing_actuals_provider_period', 'billing_actuals', ['provider', 'period_start'], unique=False)
    op.create_table('billing_connections',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('provider_type', sa.String(length=32), nullable=False),
    sa.Column('provider_name', sa.String(length=256), nullable=False),
    sa.Column('service_type', sa.String(length=32), nullable=True),
    sa.Column('api_endpoint', sa.String(length=1024), nullable=True),
    sa.Column('auth_type', sa.String(length=32), nullable=False),
    sa.Column('credentials_encrypted', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('sync_schedule', sa.String(length=32), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_by', sa.String(length=256), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('consortium_peers',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('peer_endpoint', sa.String(length=512), nullable=False),
    sa.Column('peer_public_key_hash', sa.String(length=64), nullable=False, comment="SHA-256 of peer's public key for request authentication."),
    sa.Column('peer_alias', sa.String(length=128), nullable=False),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('registered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cost_centers',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False, comment='External cost-center code (e.g. CC-4100, ENG-ML)'),
    sa.Column('department', sa.String(length=256), nullable=True, comment='Department name for grouping (e.g. Engineering, Sales)'),
    sa.Column('division', sa.String(length=256), nullable=True, comment='Division within department (e.g. Platform, Product, Infrastructure)'),
    sa.Column('budget_owner_name', sa.String(length=256), nullable=True, comment="Name of the person responsible for this cost-center's budget"),
    sa.Column('budget_owner_email', sa.String(length=320), nullable=True, comment='Email of budget owner — used for scheduled report delivery'),
    sa.Column('parent_id', sa.UUID(as_uuid=False), nullable=True, comment='Parent cost-center for hierarchical org structures'),
    sa.Column('budget_monthly_usd', sa.Numeric(precision=18, scale=8), nullable=True, comment='Monthly budget allocation for this cost-center'),
    sa.Column('budget_quarterly_usd', sa.Numeric(precision=18, scale=8), nullable=True, comment='Quarterly budget allocation for this cost-center'),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['cost_centers.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('cot_ledger_entries',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False, comment='Team this decision applies to. No FK — entries survive team deletion.'),
    sa.Column('seq_num', sa.Integer(), nullable=False, comment='Position in the hash chain for this team.'),
    sa.Column('prev_hash', sa.String(length=64), nullable=True, comment='SHA-256 of previous entry. Null for genesis entry.'),
    sa.Column('entry_hash', sa.String(length=64), nullable=False, comment='SHA-256(prev_hash + decision_type + trigger + evidence_hash + summary).'),
    sa.Column('decision_type', sa.String(length=32), nullable=False, comment='governance_proposal | evolution_proposal | anomaly_signal | policy_applied | policy_dismissed'),
    sa.Column('trigger', sa.String(length=64), nullable=False, comment='What started the analysis: pattern_detection, evolution_cycle, rewind_event, anomaly_detection, manual'),
    sa.Column('decision_summary', sa.Text(), nullable=False, comment='Human-readable summary of the final decision.'),
    sa.Column('evidence_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Data points analyzed: costs, call counts, breach counts, thresholds.'),
    sa.Column('rules_evaluated', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='[{rule, fired, confidence, detail}] — which detection rules ran.'),
    sa.Column('reasoning_steps', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Ordered list of logical inferences that led to the decision.'),
    sa.Column('alternatives_considered', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Other actions evaluated and why they were rejected.'),
    sa.Column('linked_proposal_id', sa.String(length=36), nullable=True, comment='GovernanceProposal or EvolutionProposal ID.'),
    sa.Column('linked_policy_id', sa.String(length=36), nullable=True, comment='GovernancePolicy ID if policy was created/modified.'),
    sa.Column('linked_evolution_gen_id', sa.String(length=36), nullable=True, comment='EvolutionGeneration ID if from evolution cycle.'),
    sa.Column('regulatory_tags', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='["eu_ai_act_article_14", "nist_ai_rmf", "iso_42001"]'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cot_decision_type', 'cot_ledger_entries', ['decision_type', 'created_at'], unique=False)
    op.create_index('ix_cot_linked_proposal', 'cot_ledger_entries', ['linked_proposal_id'], unique=False)
    op.create_index('ix_cot_team_created', 'cot_ledger_entries', ['team_id', 'created_at'], unique=False)
    op.create_index('ix_cot_team_seq', 'cot_ledger_entries', ['team_id', 'seq_num'], unique=False)
    op.create_table('federation_consent',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('participation_mode', sa.String(length=16), nullable=False, comment='consumer | participant | consortium_hub'),
    sa.Column('disclosure_text_hash', sa.String(length=64), nullable=False, comment='SHA-256 of the consent disclosure text shown to operator.'),
    sa.Column('consented_by', sa.String(length=256), nullable=False),
    sa.Column('consented_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('instance_nonce_key', sa.String(length=64), nullable=False, comment='Rolling key for anonymous submission nonces.'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('federation_merged_results',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('result_version', sa.String(length=16), nullable=False),
    sa.Column('gene_improvements', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Gene name -> improvement delta (numeric only, no policy content).'),
    sa.Column('participating_instances', sa.Integer(), server_default='0', nullable=False, comment='Aggregate count of participating instances. No identifiers.'),
    sa.Column('confidence_score', sa.Float(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('federation_sync_log',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('direction', sa.String(length=8), nullable=False, comment='submit | receive'),
    sa.Column('status', sa.String(length=16), nullable=False, comment='success | error | skipped'),
    sa.Column('delta_id', sa.String(length=64), nullable=True),
    sa.Column('generation_from', sa.Integer(), nullable=True),
    sa.Column('generation_to', sa.Integer(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fed_sync_synced', 'federation_sync_log', ['synced_at'], unique=False)
    op.create_table('hash_chain_state',
    sa.Column('chain_name', sa.String(length=64), nullable=False),
    sa.Column('last_seq', sa.BigInteger(), nullable=False),
    sa.Column('last_hash', sa.String(length=64), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('chain_name')
    )
    op.create_table('merkle_roots',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('batch_id', sa.String(length=64), nullable=False),
    sa.Column('root_hash', sa.String(length=64), nullable=False),
    sa.Column('leaf_count', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('batch_id')
    )
    op.create_index('ix_merkle_time', 'merkle_roots', ['created_at'], unique=False)
    op.create_table('notification_configs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('scope', sa.String(length=64), nullable=False),
    sa.Column('config_json', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('scope')
    )
    op.create_table('pricing_models',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('resource_type', sa.String(length=64), nullable=False),
    sa.Column('input_cost_per_1k', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('output_cost_per_1k', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('flat_cost_per_call', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('effective_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source', sa.String(length=256), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'model', 'effective_from', name='uq_pricing_model')
    )
    op.create_index('ix_pricing_model_lookup', 'pricing_models', ['provider', 'model'], unique=False)
    op.create_table('rbac_roles',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('allow', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='Permissions this role grants'),
    sa.Column('deny', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False, comment='Permissions explicitly denied (overrides allow)'),
    sa.Column('is_system', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('routing_outcomes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('fingerprint_hash', sa.String(length=64), nullable=False),
    sa.Column('app_id', sa.String(length=36), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=True),
    sa.Column('routed_to', sa.String(length=16), nullable=False),
    sa.Column('cheap_model', sa.String(length=128), nullable=True),
    sa.Column('expensive_model', sa.String(length=128), nullable=True),
    sa.Column('input_token_count', sa.Integer(), nullable=True),
    sa.Column('output_token_count', sa.Integer(), nullable=True),
    sa.Column('input_token_bucket', sa.Integer(), nullable=True),
    sa.Column('structural_check_passed', sa.Boolean(), nullable=True),
    sa.Column('conformal_check_passed', sa.Boolean(), nullable=True),
    sa.Column('validator_passed', sa.Boolean(), nullable=True),
    sa.Column('escalation_reason', sa.String(length=64), nullable=True),
    sa.Column('cheap_latency_ms', sa.Integer(), nullable=True),
    sa.Column('expensive_latency_ms', sa.Integer(), nullable=True),
    sa.Column('cost_cheap', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('cost_expensive', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('cost_saved', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('input_embedding_norm', sa.Float(), nullable=True),
    sa.Column('nonconformity_score', sa.Float(), nullable=True),
    sa.Column('sampled_user_message', sa.Text(), nullable=True),
    sa.Column('layer', sa.String(length=16), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_routing_out_app', 'routing_outcomes', ['app_id'], unique=False)
    op.create_index('ix_routing_out_created', 'routing_outcomes', ['created_at'], unique=False)
    op.create_index('ix_routing_out_fp', 'routing_outcomes', ['fingerprint_hash'], unique=False)
    op.create_table('scenario_configs',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False, comment='Human-readable scenario name'),
    sa.Column('scenario_type', sa.String(length=32), nullable=False, comment='Type: model_swap, team_add, usage_scale, budget_change'),
    sa.Column('parameters', sa.JSON(), nullable=False, comment='Scenario parameters (varies by type)'),
    sa.Column('result', sa.JSON(), nullable=True, comment='Last computed result: projected delta, affected teams, etc.'),
    sa.Column('created_by', sa.String(length=256), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('system_settings',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.String(length=128), server_default='system', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('trajectory_decisions',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('simulation_method', sa.String(length=32), nullable=False, comment='linear, monte_carlo, slm'),
    sa.Column('p50_cost', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('p95_cost', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('breach_probability', sa.Float(), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False, comment='allow, block, degrade'),
    sa.Column('suggested_action', sa.String(length=256), nullable=True),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_trajdec_app_time', 'trajectory_decisions', ['app_id', 'computed_at'], unique=False)
    op.create_index('ix_trajdec_session', 'trajectory_decisions', ['session_id'], unique=False)
    op.create_index(op.f('ix_trajectory_decisions_session_id'), 'trajectory_decisions', ['session_id'], unique=False)
    op.create_table('trism_patterns',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('pattern_name', sa.String(length=128), nullable=False),
    sa.Column('threat_type', sa.String(length=64), nullable=False),
    sa.Column('detection_rules_json', sa.Text(), nullable=True, comment='JSON rules for this pattern.'),
    sa.Column('risk_weight', sa.Float(), server_default='1.0', nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('source', sa.String(length=32), server_default='builtin', nullable=False, comment='builtin, custom, owasp'),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('pattern_name', name='uq_trism_pattern_name')
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('external_id', sa.String(length=256), nullable=True, comment='SSO/IdP subject identifier (JWT sub claim)'),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=256), nullable=False),
    sa.Column('avatar_url', sa.String(length=1024), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('scim_enterprise_ext', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('external_id')
    )
    op.create_table('chargeback_invoices',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('cost_center_id', sa.UUID(as_uuid=False), nullable=True, comment='Cost-center this invoice covers. NULL = org-wide.'),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('total_cost_usd', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('line_items', sa.JSON(), nullable=True, comment='Structured breakdown: [{team, app, provider, model, cost, tokens}]'),
    sa.Column('format', sa.String(length=8), server_default='json', nullable=False, comment='Output format: json, csv, pdf'),
    sa.Column('status', sa.String(length=16), server_default='generated', nullable=False, comment='generated, delivered, acknowledged'),
    sa.Column('delivered_to', sa.Text(), nullable=True, comment='Delivery destination (email, Slack channel, webhook URL)'),
    sa.Column('generated_by', sa.String(length=256), nullable=True, comment='User or system that triggered generation'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['cost_center_id'], ['cost_centers.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('finance_reports',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False, comment="Human-readable report name (e.g. 'Monthly Engineering AI Spend')"),
    sa.Column('report_type', sa.String(length=32), nullable=False, comment='Type: chargeback, burn_rate, variance, audit_trail'),
    sa.Column('schedule', sa.String(length=32), nullable=False, comment='Frequency: daily, weekly, monthly, quarterly'),
    sa.Column('cost_center_id', sa.UUID(as_uuid=False), nullable=True, comment='Scope to specific cost-center. NULL = all.'),
    sa.Column('delivery_channel', sa.String(length=32), server_default='email', nullable=False, comment='Delivery method: email, slack, webhook'),
    sa.Column('delivery_target', sa.Text(), nullable=False, comment='Destination: email address, Slack channel, webhook URL'),
    sa.Column('format', sa.String(length=8), server_default='pdf', nullable=False, comment='Output format: json, csv, pdf'),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=256), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['cost_center_id'], ['cost_centers.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('teams',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('department', sa.String(length=256), nullable=True, comment="Department or division this team belongs to (e.g., 'Engineering', 'Marketing')."),
    sa.Column('registration_token_hash', sa.String(length=128), nullable=True),
    sa.Column('registration_token_prefix', sa.String(length=24), nullable=True),
    sa.Column('max_budget_usd', sa.Numeric(precision=18, scale=8), nullable=True, comment='Max daily budget for the entire team. None = unlimited.'),
    sa.Column('budget_duration', sa.String(length=16), nullable=True, comment="Budget period: 'daily', 'monthly', '30d', '1h'. Default: daily."),
    sa.Column('current_spend_usd', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False, comment='Rolling spend counter for the current budget_duration window.'),
    sa.Column('budget_monthly_usd', sa.Numeric(precision=18, scale=8), nullable=True, comment='Monthly budget allocation for burn-rate tracking.'),
    sa.Column('budget_quarterly_usd', sa.Numeric(precision=18, scale=8), nullable=True, comment='Quarterly budget allocation for EOQ projections.'),
    sa.Column('cost_center_id', sa.UUID(as_uuid=False), nullable=True, comment='Direct cost-center assignment (alternative to team_cost_centers mapping).'),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('parent_id', sa.UUID(as_uuid=False), nullable=True, comment='Parent team ID for hierarchical team structure. NULL = root.'),
    sa.ForeignKeyConstraint(['cost_center_id'], ['cost_centers.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['parent_id'], ['teams.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('apps',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.String(length=128), nullable=False),
    sa.Column('app_name', sa.String(length=256), nullable=False),
    sa.Column('environment', sa.String(length=32), nullable=False),
    sa.Column('api_key_hash', sa.String(length=128), nullable=False),
    sa.Column('api_key_prefix', sa.String(length=16), nullable=False),
    sa.Column('agent_version', sa.String(length=32), nullable=True),
    sa.Column('sdk_versions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pause_endpoint_url', sa.String(length=512), nullable=True),
    sa.Column('enforcement_state', sa.String(length=32), nullable=False),
    sa.Column('enforcement_suspended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('enforcement_suspended_reason', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('parent_hierarchy', sa.String(length=128), nullable=True, comment="Hierarchy path e.g. 'team:proj:user123' for nested budgets."),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('team_id', 'app_id', name='uq_apps_team_app_id')
    )
    op.create_index('ix_apps_api_key_hash', 'apps', ['api_key_hash'], unique=False)
    op.create_index('ix_apps_team_id', 'apps', ['team_id'], unique=False)
    op.create_table('evolution_generations',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('generation_number', sa.Integer(), nullable=False),
    sa.Column('population_size', sa.Integer(), nullable=False),
    sa.Column('best_fitness', sa.Float(), nullable=False),
    sa.Column('avg_fitness', sa.Float(), nullable=False),
    sa.Column('best_genome_yaml', sa.Text(), nullable=False),
    sa.Column('mutations_applied', sa.Text(), nullable=True, comment='JSON list of mutation descriptions.'),
    sa.Column('simulation_stats_json', sa.Text(), nullable=True, comment='JSON: agent_count, steps, cost_savings.'),
    sa.Column('prover_results_json', sa.Text(), nullable=True, comment='JSON: Z3 proof results for best genome.'),
    sa.Column('elapsed_ms', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_evogen_created', 'evolution_generations', ['created_at'], unique=False)
    op.create_index('ix_evogen_team', 'evolution_generations', ['team_id'], unique=False)
    op.create_table('federation_peers',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('peer_url', sa.String(length=512), nullable=False),
    sa.Column('api_key_hash', sa.String(length=128), nullable=False, comment='SHA-256 hex digest of the peer API key.'),
    sa.Column('api_key_prefix', sa.String(length=8), nullable=False, comment='First 8 characters of the API key for display.'),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False, comment='active | paused | error | unreachable'),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_heartbeat_latency_ms', sa.Integer(), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sync_status', sa.String(length=16), nullable=True, comment='success | error'),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Aggregate metrics from peer.'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fed_peer_status', 'federation_peers', ['status'], unique=False)
    op.create_index('ix_fed_peer_team', 'federation_peers', ['team_id'], unique=False)
    op.create_table('forecast_configs',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=True, comment='Team scope. NULL = org-wide default.'),
    sa.Column('method', sa.String(length=32), server_default='auto', nullable=False, comment='Forecast method: auto, builtin, custom_ml, ai'),
    sa.Column('custom_ml_url', sa.Text(), nullable=True, comment="URL of customer's ML prediction endpoint"),
    sa.Column('custom_ml_auth_encrypted', sa.Text(), nullable=True, comment='Fernet-encrypted Authorization header for ML endpoint'),
    sa.Column('custom_ml_timeout_seconds', sa.Integer(), server_default=sa.text('30'), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_by', sa.String(length=256), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('team_id')
    )
    op.create_table('hndl_risk_assessments',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('algorithm_detected', sa.String(length=64), nullable=False),
    sa.Column('key_size_bits', sa.Integer(), nullable=False),
    sa.Column('estimated_quantum_break_year', sa.Integer(), nullable=False),
    sa.Column('risk_level', sa.String(length=16), nullable=False, comment='safe | monitor | urgent | critical'),
    sa.Column('data_sensitivity', sa.String(length=32), server_default=sa.text("'standard'"), nullable=False, comment='standard | confidential | secret | top_secret'),
    sa.Column('recommendation', sa.Text(), nullable=False),
    sa.Column('enforcement_action', sa.String(length=16), server_default=sa.text("'allowed'"), nullable=False, comment='allowed | warned | degraded | blocked'),
    sa.Column('assessed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_hndl_risk_level', 'hndl_risk_assessments', ['team_id', 'risk_level'], unique=False)
    op.create_index('ix_hndl_risk_team_assessed', 'hndl_risk_assessments', ['team_id', 'assessed_at'], unique=False)
    op.create_table('invitations',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('role_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=True, comment='Team scope for the assigned role. NULL = platform-level.'),
    sa.Column('invited_by', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('token_hash', sa.String(length=128), nullable=False, comment='bcrypt hash of the invitation token'),
    sa.Column('token_prefix', sa.String(length=24), nullable=False, comment='First chars of token for identification'),
    sa.Column('channel', sa.String(length=32), server_default='email', nullable=False, comment='Delivery channel: email|slack|teams'),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False, comment='pending|accepted|expired|revoked'),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['invited_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['role_id'], ['rbac_roles.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('poe_merkle_anchors',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('merkle_root', sa.String(length=64), nullable=False),
    sa.Column('entry_count', sa.Integer(), nullable=False),
    sa.Column('first_entry_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('last_entry_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('anchored_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_poe_anchor_team', 'poe_merkle_anchors', ['team_id', 'anchored_at'], unique=False)
    op.create_table('rbac_assignments',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('user_id', sa.String(length=256), nullable=False),
    sa.Column('role_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('assigned_by', sa.String(length=256), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['rbac_roles.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'role_id', 'team_id', name='uq_rbac_assignment')
    )
    op.create_index('ix_rbac_team_id', 'rbac_assignments', ['team_id'], unique=False)
    op.create_index('ix_rbac_user_id', 'rbac_assignments', ['user_id'], unique=False)
    op.create_table('spend_forecasts',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('period_label', sa.String(length=32), nullable=False),
    sa.Column('mtd_actual', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('forecast_eom', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('forecast_eoq', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('forecast_eoy', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('trend_pct', sa.Numeric(precision=8, scale=4), nullable=True),
    sa.Column('r_squared', sa.Numeric(precision=6, scale=4), nullable=True),
    sa.Column('basis_days', sa.Integer(), server_default='28', nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_forecast_team_computed', 'spend_forecasts', ['team_id', 'computed_at'], unique=False)
    op.create_table('swarm_configs',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('swarm_name', sa.String(length=256), nullable=False),
    sa.Column('party_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='List of party identifiers (hashed, no PII).'),
    sa.Column('shared_policy_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='List of governance_policies IDs for joint evaluation.'),
    sa.Column('threshold_k', sa.Integer(), nullable=False, comment='Minimum parties required for threshold sign-off (k).'),
    sa.Column('total_parties_n', sa.Integer(), nullable=False),
    sa.Column('evaluation_mode', sa.String(length=16), server_default='additive', nullable=False, comment='additive | shamir'),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('team_cost_centers',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('cost_center_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('allocation_pct', sa.Numeric(precision=5, scale=2), server_default='100.00', nullable=False, comment="Percentage of this team's cost allocated to this cost-center (for split billing)"),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['cost_center_id'], ['cost_centers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('team_id', name='uq_team_cost_center_team')
    )
    op.create_table('team_memberships',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('user_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('added_by', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'team_id', name='uq_team_membership')
    )
    op.create_index('ix_tm_team_id', 'team_memberships', ['team_id'], unique=False)
    op.create_index('ix_tm_user_id', 'team_memberships', ['user_id'], unique=False)
    op.create_table('user_preferences',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('user_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('default_team_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('default_view', sa.String(length=32), nullable=True, comment='Preferred dashboard view: overview|devops|executive|billing'),
    sa.Column('pinned_app_ids', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False, comment='List of app UUIDs the user is responsible for'),
    sa.Column('dashboard_layout', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Custom widget layout / column preferences'),
    sa.Column('notification_prefs', postgresql.JSONB(astext_type=sa.Text()), server_default='{"email": true, "in_app": true}', nullable=False, comment='Per-channel notification opt-in'),
    sa.Column('timezone', sa.String(length=64), server_default='UTC', nullable=True),
    sa.Column('theme', sa.String(length=16), server_default='system', nullable=True, comment='UI theme: light|dark|system'),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['default_team_id'], ['teams.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id')
    )
    op.create_table('agent_heartbeats',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('agent_version', sa.String(length=32), nullable=True),
    sa.Column('instrumented_providers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('host_info', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_heartbeats_app_id_ts', 'agent_heartbeats', ['app_id', 'received_at'], unique=False)
    op.create_table('agent_identities',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('agent_fingerprint', sa.String(length=64), nullable=False, comment='SHA-256 fingerprint of agent public key'),
    sa.Column('public_key_hash', sa.String(length=64), nullable=False),
    sa.Column('key_algorithm', sa.String(length=32), server_default=sa.text("'hmac-shim-v1'"), nullable=False, comment='ml-kem-768 | hmac-shim-v1'),
    sa.Column('platform_chain_hash', sa.String(length=64), nullable=False, comment='Hash linking to platform root key chain'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_fingerprint', name='uq_agent_fingerprint')
    )
    op.create_index('ix_agent_identity_team_app', 'agent_identities', ['team_id', 'app_id'], unique=False)
    op.create_table('anomaly_events',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('metric', sa.String(length=32), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('z_score', sa.Numeric(precision=8, scale=4), nullable=False),
    sa.Column('baseline_value', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('actual_value', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('window_hours', sa.Integer(), server_default='1', nullable=False),
    sa.Column('ai_explanation', sa.Text(), nullable=True),
    sa.Column('detected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledged_by', sa.String(length=128), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_anomaly_app_detected', 'anomaly_events', ['app_id', 'detected_at'], unique=False)
    op.create_index('ix_anomaly_severity', 'anomaly_events', ['severity', 'detected_at'], unique=False)
    op.create_index('ix_anomaly_team_detected', 'anomaly_events', ['team_id', 'detected_at'], unique=False)
    op.create_table('app_topology',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('runtime', sa.String(length=32), nullable=True),
    sa.Column('python_version', sa.String(length=16), nullable=True),
    sa.Column('platform', sa.String(length=64), nullable=True),
    sa.Column('deployment_type', sa.String(length=32), nullable=True),
    sa.Column('cloud_provider', sa.String(length=32), nullable=True),
    sa.Column('detected_environment', sa.String(length=32), nullable=True),
    sa.Column('k8s_flavor', sa.String(length=32), nullable=True),
    sa.Column('container_name', sa.String(length=256), nullable=True),
    sa.Column('k8s_namespace', sa.String(length=256), nullable=True),
    sa.Column('k8s_deployment', sa.String(length=256), nullable=True),
    sa.Column('web_framework', sa.String(length=64), nullable=True),
    sa.Column('ai_providers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ai_frameworks', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('api_routes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('service_dependencies', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('infrastructure_packages', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('worker_count', sa.Integer(), nullable=True),
    sa.Column('hostname', sa.String(length=256), nullable=True),
    sa.Column('ai_summary', sa.Text(), nullable=True),
    sa.Column('ai_summary_generated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('snapshot_hash', sa.String(length=64), nullable=True),
    sa.Column('raw_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('app_id')
    )
    op.create_index('ix_topology_app_id', 'app_topology', ['app_id'], unique=False)
    op.create_index('ix_topology_team_id', 'app_topology', ['team_id'], unique=False)
    op.create_table('attribution_sessions',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('framework_tier', sa.String(length=16), nullable=False, comment="'structured' (Tier 1) or 'custom' (Tier 2)"),
    sa.Column('status', sa.String(length=16), nullable=False, comment="'pending' | 'processed' | 'stale'"),
    sa.Column('total_cost', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False),
    sa.Column('total_calls', sa.Integer(), server_default='0', nullable=False),
    sa.Column('total_input_tokens', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('total_output_tokens', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('retry_cost', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False, comment='Total cost of retry calls in this session.'),
    sa.Column('defensive_cost', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False, comment='Total cost of fallback/defensive subgraph calls.'),
    sa.Column('attribution_confidence', sa.Float(), nullable=True, comment='0.0-1.0 confidence in attribution accuracy.'),
    sa.Column('graph_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Serialized call graph for trace visualization.'),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_attribution_sessions_session_id'), 'attribution_sessions', ['session_id'], unique=True)
    op.create_index('ix_attrsess_app_started', 'attribution_sessions', ['app_id', 'started_at'], unique=False)
    op.create_index('ix_attrsess_status', 'attribution_sessions', ['status'], unique=False)
    op.create_index('ix_attrsess_team_started', 'attribution_sessions', ['team_id', 'started_at'], unique=False)
    op.create_table('evolution_proposals',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('generation_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('proposal_yaml', sa.Text(), nullable=False),
    sa.Column('fitness_score', sa.Float(), nullable=False),
    sa.Column('constitutional_diff', sa.Text(), nullable=False, comment='Human-readable diff from current policies.'),
    sa.Column('rationale', sa.Text(), nullable=True, comment='LLM-generated rationale if Tier 3.'),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False, comment='pending, accepted, rejected'),
    sa.Column('accepted_by', sa.String(length=256), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['generation_id'], ['evolution_generations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_evoprop_team_status', 'evolution_proposals', ['team_id', 'status'], unique=False)
    op.create_table('federation_peer_sync_log',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('peer_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=False, comment='push | pull | heartbeat'),
    sa.Column('status', sa.String(length=16), nullable=False, comment='success | error | timeout'),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['peer_id'], ['federation_peers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fed_psync_peer_at', 'federation_peer_sync_log', ['peer_id', 'synced_at'], unique=False)
    op.create_table('governance_policies',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('policy_type', sa.String(length=32), nullable=False),
    sa.Column('effect', sa.String(length=16), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('suggested_model', sa.String(length=128), nullable=True),
    sa.Column('conditions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('action', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=256), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_policies_active', 'governance_policies', ['is_active'], unique=False)
    op.create_index('ix_policies_app_id', 'governance_policies', ['app_id'], unique=False)
    op.create_index('ix_policies_scope_priority', 'governance_policies', ['scope', 'priority'], unique=False)
    op.create_index('ix_policies_team_id', 'governance_policies', ['team_id'], unique=False)
    op.create_table('governance_proposals',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True, comment='Null for team-wide proposals.'),
    sa.Column('proposal_type', sa.String(length=64), nullable=False, comment='model_downshift, budget_tighten, model_denylist, amplification_gate, time_policy, rate_limit_suggest, rewind_learned'),
    sa.Column('severity', sa.String(length=16), server_default='info', nullable=False, comment='info, warning, critical'),
    sa.Column('title', sa.String(length=512), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('current_yaml', sa.Text(), nullable=True, comment='Existing policy YAML if modifying an existing policy.'),
    sa.Column('proposed_yaml', sa.Text(), nullable=False, comment='Proposed policy YAML to apply.'),
    sa.Column('data_snapshot', sa.JSON(), nullable=True, comment='Usage data that triggered this proposal.'),
    sa.Column('estimated_savings_usd', sa.Numeric(precision=18, scale=8), nullable=True, comment='Estimated monthly savings if proposal is applied.'),
    sa.Column('source', sa.String(length=32), server_default='rule_engine', nullable=False, comment='rule_engine, rewind, slm'),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False, comment='pending, applied, dismissed, expired'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('applied_by', sa.String(length=256), nullable=True),
    sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dismissed_reason', sa.Text(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True, comment='Auto-expire stale proposals.'),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_govprop_created', 'governance_proposals', ['created_at'], unique=False)
    op.create_index('ix_govprop_team_status', 'governance_proposals', ['team_id', 'status'], unique=False)
    op.create_table('ingest_batches',
    sa.Column('batch_id', sa.String(length=64), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('record_count', sa.Integer(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.PrimaryKeyConstraint('batch_id')
    )
    op.create_index('ix_ingest_batch_received', 'ingest_batches', ['received_at'], unique=False)
    op.create_table('neuro_assurance_metrics',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('session_id', sa.String(length=64), nullable=True),
    sa.Column('topology_hash', sa.String(length=64), nullable=False),
    sa.Column('energy_per_spike', sa.Float(), nullable=True),
    sa.Column('stdp_drift', sa.Float(), nullable=True, comment='Spike-timing-dependent plasticity drift metric.'),
    sa.Column('embodied_efficiency_score', sa.Float(), nullable=True, comment='Calls per watt-hour equivalent.'),
    sa.Column('spike_rate_hz', sa.Float(), nullable=True),
    sa.Column('active_neuron_ratio', sa.Float(), nullable=True),
    sa.Column('inference_latency_ms', sa.Float(), nullable=True),
    sa.Column('hardware_backend', sa.String(length=16), server_default='software', nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_neuro_assurance_app', 'neuro_assurance_metrics', ['app_id', 'recorded_at'], unique=False)
    op.create_index('ix_neuro_assurance_team', 'neuro_assurance_metrics', ['team_id', 'recorded_at'], unique=False)
    op.create_table('neuro_compliance_reports',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('metric_count', sa.Integer(), nullable=False),
    sa.Column('from_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('to_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('neuromorphic_metrics',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('evaluation_count', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('avg_latency_ms', sa.Float(), nullable=False),
    sa.Column('avg_power_mw', sa.Float(), nullable=True, comment='Hardware-only. Null for software emulation.'),
    sa.Column('spike_efficiency', sa.Float(), nullable=False, comment='Ratio of active neurons to total.'),
    sa.Column('topology_hash', sa.String(length=64), nullable=False, comment='SHA-256 of compiled SNN topology.'),
    sa.Column('hardware_backend', sa.String(length=32), server_default='software', nullable=False, comment='software, loihi, speck, none'),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_neurometric_app', 'neuromorphic_metrics', ['app_id'], unique=False)
    op.create_index('ix_neurometric_period', 'neuromorphic_metrics', ['period_start'], unique=False)
    op.create_table('optimization_recommendations',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('current_model', sa.String(length=128), nullable=False),
    sa.Column('suggested_model', sa.String(length=128), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('call_volume_basis', sa.BigInteger(), nullable=False),
    sa.Column('estimated_monthly_savings', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('confidence', sa.String(length=16), server_default='medium', nullable=False),
    sa.Column('recommendation_text', sa.Text(), nullable=True),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_rec_savings', 'optimization_recommendations', ['estimated_monthly_savings'], unique=False)
    op.create_index('ix_rec_team_app', 'optimization_recommendations', ['team_id', 'app_id'], unique=False)
    op.create_table('pqc_readiness_scores',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('score', sa.Float(), nullable=False, comment='PQC readiness score 0-100'),
    sa.Column('classical_key_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('hybrid_key_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('pqc_key_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('weakest_algorithm', sa.String(length=64), nullable=True),
    sa.Column('recommendations', sa.Text(), nullable=True, comment='JSON array of recommended actions'),
    sa.Column('assessed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_pqc_readiness_app', 'pqc_readiness_scores', ['app_id', 'assessed_at'], unique=False)
    op.create_index('ix_pqc_readiness_team_assessed', 'pqc_readiness_scores', ['team_id', 'assessed_at'], unique=False)
    op.create_table('pricing_overrides',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=256), nullable=False),
    sa.Column('resource_type', sa.String(length=64), nullable=False),
    sa.Column('input_cost_per_1k', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('output_cost_per_1k', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('per_unit_cost', sa.Numeric(precision=18, scale=8), nullable=True, comment='For non-token resources: queries, API calls, etc.'),
    sa.Column('unit_label', sa.String(length=64), nullable=True, comment="Human label for the unit, e.g. 'query', 'request'."),
    sa.Column('override_reason', sa.String(length=512), nullable=True, comment='Why this override exists. Shown in audit log.'),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('app_id', 'team_id', 'provider', 'model', 'resource_type', name='uq_pricing_override')
    )
    op.create_index('ix_pricing_app_provider', 'pricing_overrides', ['app_id', 'provider'], unique=False)
    op.create_index('ix_pricing_team_provider', 'pricing_overrides', ['team_id', 'provider'], unique=False)
    op.create_table('real_time_spend',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('period', sa.String(length=16), nullable=False),
    sa.Column('window_key', sa.String(length=32), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('total_cost', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('call_count', sa.BigInteger(), nullable=False),
    sa.Column('input_tokens', sa.BigInteger(), nullable=False),
    sa.Column('output_tokens', sa.BigInteger(), nullable=False),
    sa.Column('total_duration_ms', sa.BigInteger(), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=True, comment='Agent session ID for session-level spend tracking.'),
    sa.Column('hierarchy_level', sa.String(length=32), server_default='app', nullable=False, comment='Hierarchy level: app | team | session.'),
    sa.Column('hierarchy_key', sa.String(length=128), nullable=True, comment='Identifier for this hierarchy level (session_id or team_id).'),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('app_id', 'period', 'window_key', name='uq_rts_app_window')
    )
    op.create_index('ix_rts_app_id', 'real_time_spend', ['app_id'], unique=False)
    op.create_index('ix_rts_lookup', 'real_time_spend', ['app_id', 'period', 'window_key'], unique=False)
    op.create_index('ix_rts_team_id', 'real_time_spend', ['team_id'], unique=False)
    op.create_index('ix_rts_window_end', 'real_time_spend', ['window_end'], unique=False)
    op.create_table('rewind_events',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('trigger_reason', sa.String(length=128), nullable=False, comment='circuit_breaker, output_validation, budget_suspension, manual'),
    sa.Column('actions_rolled_back', sa.JSON(), nullable=True, comment='List of {tool_name, success, error} dicts.'),
    sa.Column('failure_context', sa.JSON(), nullable=True, comment='Tool call sequence, error details, tokens consumed.'),
    sa.Column('policy_diff_generated', sa.Text(), nullable=True, comment='If distillation produced a governance proposal, its YAML.'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_rewind_app_time', 'rewind_events', ['app_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_rewind_events_session_id'), 'rewind_events', ['session_id'], unique=False)
    op.create_index('ix_rewind_team_time', 'rewind_events', ['team_id', 'created_at'], unique=False)
    op.create_table('routing_fingerprints',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('fingerprint_hash', sa.String(length=64), nullable=False),
    sa.Column('system_prompt_hash', sa.String(length=64), nullable=False),
    sa.Column('call_site_id', sa.String(length=256), nullable=True),
    sa.Column('task_type', sa.String(length=32), nullable=True),
    sa.Column('phase', sa.String(length=20), server_default='observe', nullable=False),
    sa.Column('observe_call_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('observe_threshold', sa.Integer(), server_default='200', nullable=False),
    sa.Column('routing_confidence', sa.Float(), server_default='0.0', nullable=False),
    sa.Column('conformal_threshold', sa.Float(), nullable=True),
    sa.Column('cheap_model', sa.String(length=128), nullable=True),
    sa.Column('expensive_model', sa.String(length=128), nullable=True),
    sa.Column('cheap_model_agreement_rate', sa.Float(), nullable=True),
    sa.Column('calibration_sample_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('calibration_centroid', sa.LargeBinary(), nullable=True),
    sa.Column('calibration_centroid_variance', sa.Float(), nullable=True),
    sa.Column('rolling_centroid', sa.LargeBinary(), nullable=True),
    sa.Column('rolling_centroid_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('calibration_centroid_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('rolling_centroid_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('drift_score', sa.Float(), server_default='0.0', nullable=False),
    sa.Column('drift_threshold', sa.Float(), nullable=True),
    sa.Column('confidence_decay_factor', sa.Float(), server_default='1.0', nullable=False),
    sa.Column('force_model', sa.String(length=128), nullable=True),
    sa.Column('allow_routing', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('max_misroute_rate', sa.Float(), server_default='0.01', nullable=False),
    sa.Column('total_routed_calls', sa.Integer(), server_default='0', nullable=False),
    sa.Column('total_escalations', sa.Integer(), server_default='0', nullable=False),
    sa.Column('total_validator_failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('input_token_bucket_bounds', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('fingerprint_hash')
    )
    op.create_index('ix_routing_fp_app', 'routing_fingerprints', ['app_id'], unique=False)
    op.create_index('ix_routing_fp_hash', 'routing_fingerprints', ['fingerprint_hash'], unique=False)
    op.create_index('ix_routing_fp_phase', 'routing_fingerprints', ['phase'], unique=False)
    op.create_table('session_budgets',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('max_budget_usd', sa.Numeric(precision=18, scale=8), nullable=False, comment='Hard cap for this session.'),
    sa.Column('current_spend_usd', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False, comment='Running spend total, updated atomically on each /evaluate allow.'),
    sa.Column('reset_at', sa.DateTime(timezone=True), nullable=True, comment='Optional: auto-reset spend at this time.'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'app_id', name='uq_session_budget')
    )
    op.create_index('ix_session_budget_app', 'session_budgets', ['app_id'], unique=False)
    op.create_index('ix_session_budget_team', 'session_budgets', ['team_id'], unique=False)
    op.create_index(op.f('ix_session_budgets_session_id'), 'session_budgets', ['session_id'], unique=False)
    op.create_table('session_fingerprints',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('fingerprint_hash', sa.String(length=64), nullable=False, comment='SHA-256 of session pattern grouping.'),
    sa.Column('avg_calls_per_session', sa.Float(), server_default='0', nullable=False),
    sa.Column('std_calls', sa.Float(), server_default='0', nullable=False),
    sa.Column('avg_cost_per_call', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False),
    sa.Column('std_cost', sa.Numeric(precision=18, scale=8), server_default='0', nullable=False),
    sa.Column('call_count_p95', sa.Integer(), server_default='0', nullable=False),
    sa.Column('branching_factor', sa.Float(), server_default='1.0', nullable=False, comment='Avg child calls per parent call. >1 = agentic branching.'),
    sa.Column('avg_tool_calls', sa.Float(), server_default='0', nullable=False),
    sa.Column('tool_failure_rate', sa.Float(), server_default='0', nullable=False),
    sa.Column('sample_count', sa.Integer(), server_default='0', nullable=False, comment='Number of sessions used to compute this fingerprint.'),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_sessfp_app', 'session_fingerprints', ['app_id'], unique=False)
    op.create_index('ix_sessfp_team_time', 'session_fingerprints', ['team_id', 'updated_at'], unique=False)
    op.create_index(op.f('ix_session_fingerprints_fingerprint_hash'), 'session_fingerprints', ['fingerprint_hash'], unique=False)
    op.create_table('swarm_sessions',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('swarm_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_ref', sa.String(length=64), nullable=False, comment='External session or request reference.'),
    sa.Column('status', sa.String(length=16), server_default='open', nullable=False, comment='open | evaluating | complete | expired'),
    sa.Column('contributions_received', sa.Integer(), server_default='0', nullable=False),
    sa.Column('mpc_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['swarm_id'], ['swarm_configs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_swarm_sess_swarm_status', 'swarm_sessions', ['swarm_id', 'status'], unique=False)
    op.create_index('ix_swarm_sess_team', 'swarm_sessions', ['team_id', 'created_at'], unique=False)
    op.create_table('thresholds',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('user_id', sa.UUID(as_uuid=False), nullable=True, comment='Per-user threshold scoping.'),
    sa.Column('cost_center_id', sa.UUID(as_uuid=False), nullable=True, comment='Per-cost-center threshold scoping.'),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=True),
    sa.Column('metric', sa.String(length=32), nullable=False),
    sa.Column('period', sa.String(length=16), nullable=False),
    sa.Column('warning_value', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('critical_value', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('degradation_model', sa.String(length=128), nullable=True, comment="Model to degrade to when threshold breached (e.g., 'gpt-4o-mini')."),
    sa.Column('degradation_enabled', sa.Boolean(), nullable=False, comment='Whether to auto-degrade instead of deny on breach.'),
    sa.Column('notify', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['cost_center_id'], ['cost_centers.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_thresholds_app_id', 'thresholds', ['app_id'], unique=False)
    op.create_index('ix_thresholds_cost_center_id', 'thresholds', ['cost_center_id'], unique=False)
    op.create_index('ix_thresholds_team_id', 'thresholds', ['team_id'], unique=False)
    op.create_index('ix_thresholds_user_id', 'thresholds', ['user_id'], unique=False)
    op.create_table('trajectory_proofs',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('proof_type', sa.String(length=32), nullable=False, comment='hash_chain, snark'),
    sa.Column('proof_status', sa.String(length=16), nullable=False, comment='valid, invalid, pending'),
    sa.Column('proof_data', sa.Text(), nullable=False, comment='Hex-encoded proof bytes.'),
    sa.Column('public_inputs_json', sa.Text(), nullable=True, comment='JSON: policy hashes, trajectory summary.'),
    sa.Column('verification_key_hash', sa.String(length=64), nullable=True),
    sa.Column('merkle_root', sa.String(length=64), nullable=True, comment='Anchored to attestation Merkle tree.'),
    sa.Column('circuit_size', sa.Integer(), server_default='0', nullable=False),
    sa.Column('prover_time_ms', sa.Integer(), server_default='0', nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_trajproof_created', 'trajectory_proofs', ['created_at'], unique=False)
    op.create_index('ix_trajproof_session', 'trajectory_proofs', ['session_id'], unique=False)
    op.create_table('usage_aggregates',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('resource_type', sa.String(length=64), nullable=False),
    sa.Column('granularity', sa.String(length=8), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('call_count', sa.BigInteger(), nullable=False),
    sa.Column('input_tokens', sa.BigInteger(), nullable=False),
    sa.Column('output_tokens', sa.BigInteger(), nullable=False),
    sa.Column('total_tokens', sa.BigInteger(), nullable=False),
    sa.Column('input_cost', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('output_cost', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('total_cost', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('avg_duration_ms', sa.Integer(), nullable=True),
    sa.Column('min_duration_ms', sa.Integer(), nullable=True),
    sa.Column('max_duration_ms', sa.Integer(), nullable=True),
    sa.Column('duration_ms_sum', sa.BigInteger(), nullable=True),
    sa.Column('p95_duration_ms', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=True),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('app_id', 'team_id', 'provider', 'model', 'period_start', 'granularity', name='uq_aggregates_key')
    )
    op.create_index('ix_agg_app_period', 'usage_aggregates', ['app_id', 'period_start', 'granularity'], unique=False)
    op.create_index('ix_agg_team_period', 'usage_aggregates', ['team_id', 'period_start', 'granularity'], unique=False)
    op.create_table('usage_records',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('resource_type', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('operation', sa.String(length=128), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('total_tokens', sa.Integer(), nullable=True),
    sa.Column('input_cost', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('output_cost', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('total_cost', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('batch_id', sa.String(length=64), nullable=True),
    sa.Column('session_id', sa.String(length=64), nullable=True, comment='Agent session ID for multi-step tracing.'),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_records_app_id_ts', 'usage_records', ['app_id', 'timestamp'], unique=False)
    op.create_index('ix_records_provider_ts', 'usage_records', ['provider', 'timestamp'], unique=False)
    op.create_index('ix_records_team_id_ts', 'usage_records', ['team_id', 'timestamp'], unique=False)
    op.create_index('ix_records_team_time', 'usage_records', ['team_id', 'timestamp', 'provider'], unique=False)
    op.create_index('ix_records_timestamp', 'usage_records', ['timestamp'], unique=False)
    op.create_index(op.f('ix_usage_records_session_id'), 'usage_records', ['session_id'], unique=False)
    op.create_table('alerts',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('threshold_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('metric', sa.String(length=32), nullable=False),
    sa.Column('threshold_value', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('actual_value', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('fired_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledged_by', sa.String(length=256), nullable=True),
    sa.Column('notification_sent', sa.Boolean(), nullable=False),
    sa.Column('notification_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.ForeignKeyConstraint(['threshold_id'], ['thresholds.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_alerts_acknowledged', 'alerts', ['acknowledged_at'], unique=False)
    op.create_index('ix_alerts_app_id_fired', 'alerts', ['app_id', 'fired_at'], unique=False)
    op.create_index('ix_alerts_team_id_fired', 'alerts', ['team_id', 'fired_at'], unique=False)
    op.create_table('poe_ledger_entries',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('seq_num', sa.Integer(), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=True, comment='SHA-256 of previous entry. Null for genesis entry.'),
    sa.Column('entry_hash', sa.String(length=64), nullable=False, comment='SHA-256(prev_hash + decision_hash + timestamp).'),
    sa.Column('decision_hash', sa.String(length=64), nullable=False, comment='SHA-256 of the enforcement decision payload.'),
    sa.Column('trajectory_proof_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('proof_type', sa.String(length=16), server_default='hash_chain', nullable=False, comment='hash_chain | snark'),
    sa.Column('tee_quote', sa.LargeBinary(), nullable=True, comment='TEE attestation quote (TDX or SEV-SNP) if hardware present.'),
    sa.Column('tee_platform', sa.String(length=16), nullable=True, comment='tdx | sev-snp | null'),
    sa.Column('risk_level', sa.String(length=16), server_default='medium', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['trajectory_proof_id'], ['trajectory_proofs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_poe_session_seq', 'poe_ledger_entries', ['session_id', 'seq_num'], unique=False)
    op.create_index('ix_poe_team_created', 'poe_ledger_entries', ['team_id', 'created_at'], unique=False)
    op.create_table('policy_decision_records',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('swarm_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('decision', sa.String(length=8), nullable=False, comment='allow | deny'),
    sa.Column('party_count', sa.Integer(), nullable=False),
    sa.Column('threshold_met', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('mpc_proof', sa.Text(), nullable=False),
    sa.Column('attestation_signature', sa.String(length=256), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['swarm_sessions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['swarm_id'], ['swarm_configs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_pdr_swarm_issued', 'policy_decision_records', ['swarm_id', 'issued_at'], unique=False)
    op.create_index('ix_pdr_team_issued', 'policy_decision_records', ['team_id', 'issued_at'], unique=False)
    op.create_table('policy_decisions',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('policy_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=512), nullable=False),
    sa.Column('request_provider', sa.String(length=64), nullable=True),
    sa.Column('request_model', sa.String(length=256), nullable=True),
    sa.Column('request_environment', sa.String(length=32), nullable=True),
    sa.Column('request_estimated_tokens', sa.Integer(), nullable=True),
    sa.Column('request_estimated_cost', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('spend_at_decision', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('spend_limit', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('evaluation_latency_ms', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.ForeignKeyConstraint(['policy_id'], ['governance_policies.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_pd_app_id_decided', 'policy_decisions', ['app_id', 'decided_at'], unique=False)
    op.create_index('ix_pd_decision', 'policy_decisions', ['decision', 'decided_at'], unique=False)
    op.create_index('ix_pd_policy_id', 'policy_decisions', ['policy_id'], unique=False)
    op.create_index('ix_pd_team_id_decided', 'policy_decisions', ['team_id', 'decided_at'], unique=False)
    op.create_table('policy_proofs',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('policy_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('proof_type', sa.String(length=16), nullable=False, comment='bounded, smt'),
    sa.Column('proof_status', sa.String(length=16), nullable=False, comment='proven, disproven, timeout, unknown'),
    sa.Column('proof_certificate', sa.Text(), nullable=True, comment='JSON proof certificate.'),
    sa.Column('counterexample', sa.Text(), nullable=True, comment='JSON showing violating trajectory if disproven.'),
    sa.Column('variables_checked', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_depth', sa.Integer(), server_default='0', nullable=False),
    sa.Column('solver_time_ms', sa.Integer(), server_default='0', nullable=False),
    sa.Column('proven_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['policy_id'], ['governance_policies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_proof_policy', 'policy_proofs', ['policy_id'], unique=False)
    op.create_table('swarm_contributions',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('party_hash', sa.String(length=64), nullable=False, comment='HMAC-SHA256 of party_id with per-swarm salt.'),
    sa.Column('share_data', sa.Text(), nullable=False, comment='Fernet-encrypted Shamir share.'),
    sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['swarm_sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'party_hash', name='uq_swarm_party')
    )
    op.create_table('trism_threat_events',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('app_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('team_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('threat_type', sa.String(length=64), nullable=False, comment='context_poisoning, goal_hijack, cascade_failure, communication_anomaly, prompt_injection, tool_misuse'),
    sa.Column('severity', sa.String(length=16), nullable=False, comment='info, warning, critical, blocked'),
    sa.Column('confidence_score', sa.Float(), nullable=False),
    sa.Column('detection_method', sa.String(length=32), nullable=False, comment='pattern, statistical, slm'),
    sa.Column('indicators_json', sa.Text(), nullable=True, comment='JSON evidence that triggered detection.'),
    sa.Column('rollback_plan_json', sa.Text(), nullable=True, comment='JSON auto-generated rollback steps.'),
    sa.Column('action_taken', sa.String(length=32), server_default='alert', nullable=False, comment='alert, block, degrade, rewind'),
    sa.Column('related_rewind_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['related_rewind_id'], ['rewind_events.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_trism_created', 'trism_threat_events', ['created_at'], unique=False)
    op.create_index('ix_trism_session', 'trism_threat_events', ['session_id'], unique=False)
    op.create_index('ix_trism_severity', 'trism_threat_events', ['severity'], unique=False)
    op.create_table('enforcement_attestations',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('decision_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('attestation_hash', sa.String(length=64), nullable=False, comment='SHA-256 of canonical attestation payload.'),
    sa.Column('signature', sa.String(length=128), nullable=False, comment='HMAC-SHA256 signature of the attestation payload.'),
    sa.Column('nonce', sa.String(length=64), nullable=False, comment='Random nonce for replay prevention.'),
    sa.Column('key_source', sa.String(length=16), server_default='env', nullable=False, comment='tpm, env, tee'),
    sa.Column('algorithm', sa.String(length=32), server_default='hmac-sha256', nullable=False),
    sa.Column('payload_json', sa.Text(), nullable=False, comment='Canonical JSON of the attestation payload.'),
    sa.Column('merkle_batch_id', sa.String(length=64), nullable=True, comment='Batch ID linking to MerkleRoot. Set on flush.'),
    sa.Column('merkle_proof', sa.JSON(), nullable=True, comment='Merkle inclusion proof (list of sibling hashes).'),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verified_by', sa.String(length=256), nullable=True),
    sa.Column('pqc_signature', sa.Text(), nullable=True, comment='PQC attestation value, hex-encoded: HMAC-SHA-512 commitment (default tier) or ML-DSA-65 signature (Tier 2). NULL until migrated.'),
    sa.Column('pqc_algorithm', sa.String(length=64), nullable=True, comment='Algorithm used (e.g. pqc-shim-hmac-sha512, ml-dsa-65).'),
    sa.Column('pqc_public_key_id', sa.String(length=64), nullable=True, comment='Stable key identifier (ML-DSA public key id, or shim key id for the HMAC tier — the HMAC tier has no public key).'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['decision_id'], ['policy_decisions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_attest_batch', 'enforcement_attestations', ['merkle_batch_id'], unique=False)
    op.create_index('ix_attest_decision', 'enforcement_attestations', ['decision_id'], unique=False)
    op.create_index('ix_attest_time', 'enforcement_attestations', ['created_at'], unique=False)
    op.create_table('notification_deliveries',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('alert_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_notif_deliveries_alert', 'notification_deliveries', ['alert_id'], unique=False)
    op.create_index('ix_notif_deliveries_status', 'notification_deliveries', ['status', 'created_at'], unique=False)
    op.create_table('pqc_migration_log',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('attestation_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('old_algorithm', sa.String(length=64), nullable=False),
    sa.Column('new_algorithm', sa.String(length=64), nullable=False),
    sa.Column('old_signature', sa.Text(), nullable=False),
    sa.Column('new_signature', sa.Text(), nullable=False),
    sa.Column('migrated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['attestation_id'], ['enforcement_attestations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_pqcmig_attestation', 'pqc_migration_log', ['attestation_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_pqcmig_attestation', table_name='pqc_migration_log')
    op.drop_table('pqc_migration_log')
    op.drop_index('ix_notif_deliveries_status', table_name='notification_deliveries')
    op.drop_index('ix_notif_deliveries_alert', table_name='notification_deliveries')
    op.drop_table('notification_deliveries')
    op.drop_index('ix_attest_time', table_name='enforcement_attestations')
    op.drop_index('ix_attest_decision', table_name='enforcement_attestations')
    op.drop_index('ix_attest_batch', table_name='enforcement_attestations')
    op.drop_table('enforcement_attestations')
    op.drop_index('ix_trism_severity', table_name='trism_threat_events')
    op.drop_index('ix_trism_session', table_name='trism_threat_events')
    op.drop_index('ix_trism_created', table_name='trism_threat_events')
    op.drop_table('trism_threat_events')
    op.drop_table('swarm_contributions')
    op.drop_index('ix_proof_policy', table_name='policy_proofs')
    op.drop_table('policy_proofs')
    op.drop_index('ix_pd_team_id_decided', table_name='policy_decisions')
    op.drop_index('ix_pd_policy_id', table_name='policy_decisions')
    op.drop_index('ix_pd_decision', table_name='policy_decisions')
    op.drop_index('ix_pd_app_id_decided', table_name='policy_decisions')
    op.drop_table('policy_decisions')
    op.drop_index('ix_pdr_team_issued', table_name='policy_decision_records')
    op.drop_index('ix_pdr_swarm_issued', table_name='policy_decision_records')
    op.drop_table('policy_decision_records')
    op.drop_index('ix_poe_team_created', table_name='poe_ledger_entries')
    op.drop_index('ix_poe_session_seq', table_name='poe_ledger_entries')
    op.drop_table('poe_ledger_entries')
    op.drop_index('ix_alerts_team_id_fired', table_name='alerts')
    op.drop_index('ix_alerts_app_id_fired', table_name='alerts')
    op.drop_index('ix_alerts_acknowledged', table_name='alerts')
    op.drop_table('alerts')
    op.drop_index(op.f('ix_usage_records_session_id'), table_name='usage_records')
    op.drop_index('ix_records_timestamp', table_name='usage_records')
    op.drop_index('ix_records_team_time', table_name='usage_records')
    op.drop_index('ix_records_team_id_ts', table_name='usage_records')
    op.drop_index('ix_records_provider_ts', table_name='usage_records')
    op.drop_index('ix_records_app_id_ts', table_name='usage_records')
    op.drop_table('usage_records')
    op.drop_index('ix_agg_team_period', table_name='usage_aggregates')
    op.drop_index('ix_agg_app_period', table_name='usage_aggregates')
    op.drop_table('usage_aggregates')
    op.drop_index('ix_trajproof_session', table_name='trajectory_proofs')
    op.drop_index('ix_trajproof_created', table_name='trajectory_proofs')
    op.drop_table('trajectory_proofs')
    op.drop_index('ix_thresholds_user_id', table_name='thresholds')
    op.drop_index('ix_thresholds_team_id', table_name='thresholds')
    op.drop_index('ix_thresholds_cost_center_id', table_name='thresholds')
    op.drop_index('ix_thresholds_app_id', table_name='thresholds')
    op.drop_table('thresholds')
    op.drop_index('ix_swarm_sess_team', table_name='swarm_sessions')
    op.drop_index('ix_swarm_sess_swarm_status', table_name='swarm_sessions')
    op.drop_table('swarm_sessions')
    op.drop_index(op.f('ix_session_fingerprints_fingerprint_hash'), table_name='session_fingerprints')
    op.drop_index('ix_sessfp_team_time', table_name='session_fingerprints')
    op.drop_index('ix_sessfp_app', table_name='session_fingerprints')
    op.drop_table('session_fingerprints')
    op.drop_index(op.f('ix_session_budgets_session_id'), table_name='session_budgets')
    op.drop_index('ix_session_budget_team', table_name='session_budgets')
    op.drop_index('ix_session_budget_app', table_name='session_budgets')
    op.drop_table('session_budgets')
    op.drop_index('ix_routing_fp_phase', table_name='routing_fingerprints')
    op.drop_index('ix_routing_fp_hash', table_name='routing_fingerprints')
    op.drop_index('ix_routing_fp_app', table_name='routing_fingerprints')
    op.drop_table('routing_fingerprints')
    op.drop_index('ix_rewind_team_time', table_name='rewind_events')
    op.drop_index(op.f('ix_rewind_events_session_id'), table_name='rewind_events')
    op.drop_index('ix_rewind_app_time', table_name='rewind_events')
    op.drop_table('rewind_events')
    op.drop_index('ix_rts_window_end', table_name='real_time_spend')
    op.drop_index('ix_rts_team_id', table_name='real_time_spend')
    op.drop_index('ix_rts_lookup', table_name='real_time_spend')
    op.drop_index('ix_rts_app_id', table_name='real_time_spend')
    op.drop_table('real_time_spend')
    op.drop_index('ix_pricing_team_provider', table_name='pricing_overrides')
    op.drop_index('ix_pricing_app_provider', table_name='pricing_overrides')
    op.drop_table('pricing_overrides')
    op.drop_index('ix_pqc_readiness_team_assessed', table_name='pqc_readiness_scores')
    op.drop_index('ix_pqc_readiness_app', table_name='pqc_readiness_scores')
    op.drop_table('pqc_readiness_scores')
    op.drop_index('ix_rec_team_app', table_name='optimization_recommendations')
    op.drop_index('ix_rec_savings', table_name='optimization_recommendations')
    op.drop_table('optimization_recommendations')
    op.drop_index('ix_neurometric_period', table_name='neuromorphic_metrics')
    op.drop_index('ix_neurometric_app', table_name='neuromorphic_metrics')
    op.drop_table('neuromorphic_metrics')
    op.drop_table('neuro_compliance_reports')
    op.drop_index('ix_neuro_assurance_team', table_name='neuro_assurance_metrics')
    op.drop_index('ix_neuro_assurance_app', table_name='neuro_assurance_metrics')
    op.drop_table('neuro_assurance_metrics')
    op.drop_index('ix_ingest_batch_received', table_name='ingest_batches')
    op.drop_table('ingest_batches')
    op.drop_index('ix_govprop_team_status', table_name='governance_proposals')
    op.drop_index('ix_govprop_created', table_name='governance_proposals')
    op.drop_table('governance_proposals')
    op.drop_index('ix_policies_team_id', table_name='governance_policies')
    op.drop_index('ix_policies_scope_priority', table_name='governance_policies')
    op.drop_index('ix_policies_app_id', table_name='governance_policies')
    op.drop_index('ix_policies_active', table_name='governance_policies')
    op.drop_table('governance_policies')
    op.drop_index('ix_fed_psync_peer_at', table_name='federation_peer_sync_log')
    op.drop_table('federation_peer_sync_log')
    op.drop_index('ix_evoprop_team_status', table_name='evolution_proposals')
    op.drop_table('evolution_proposals')
    op.drop_index('ix_attrsess_team_started', table_name='attribution_sessions')
    op.drop_index('ix_attrsess_status', table_name='attribution_sessions')
    op.drop_index('ix_attrsess_app_started', table_name='attribution_sessions')
    op.drop_index(op.f('ix_attribution_sessions_session_id'), table_name='attribution_sessions')
    op.drop_table('attribution_sessions')
    op.drop_index('ix_topology_team_id', table_name='app_topology')
    op.drop_index('ix_topology_app_id', table_name='app_topology')
    op.drop_table('app_topology')
    op.drop_index('ix_anomaly_team_detected', table_name='anomaly_events')
    op.drop_index('ix_anomaly_severity', table_name='anomaly_events')
    op.drop_index('ix_anomaly_app_detected', table_name='anomaly_events')
    op.drop_table('anomaly_events')
    op.drop_index('ix_agent_identity_team_app', table_name='agent_identities')
    op.drop_table('agent_identities')
    op.drop_index('ix_heartbeats_app_id_ts', table_name='agent_heartbeats')
    op.drop_table('agent_heartbeats')
    op.drop_table('user_preferences')
    op.drop_index('ix_tm_user_id', table_name='team_memberships')
    op.drop_index('ix_tm_team_id', table_name='team_memberships')
    op.drop_table('team_memberships')
    op.drop_table('team_cost_centers')
    op.drop_table('swarm_configs')
    op.drop_index('ix_forecast_team_computed', table_name='spend_forecasts')
    op.drop_table('spend_forecasts')
    op.drop_index('ix_rbac_user_id', table_name='rbac_assignments')
    op.drop_index('ix_rbac_team_id', table_name='rbac_assignments')
    op.drop_table('rbac_assignments')
    op.drop_index('ix_poe_anchor_team', table_name='poe_merkle_anchors')
    op.drop_table('poe_merkle_anchors')
    op.drop_table('invitations')
    op.drop_index('ix_hndl_risk_team_assessed', table_name='hndl_risk_assessments')
    op.drop_index('ix_hndl_risk_level', table_name='hndl_risk_assessments')
    op.drop_table('hndl_risk_assessments')
    op.drop_table('forecast_configs')
    op.drop_index('ix_fed_peer_team', table_name='federation_peers')
    op.drop_index('ix_fed_peer_status', table_name='federation_peers')
    op.drop_table('federation_peers')
    op.drop_index('ix_evogen_team', table_name='evolution_generations')
    op.drop_index('ix_evogen_created', table_name='evolution_generations')
    op.drop_table('evolution_generations')
    op.drop_index('ix_apps_team_id', table_name='apps')
    op.drop_index('ix_apps_api_key_hash', table_name='apps')
    op.drop_table('apps')
    op.drop_table('teams')
    op.drop_table('finance_reports')
    op.drop_table('chargeback_invoices')
    op.drop_table('users')
    op.drop_table('trism_patterns')
    op.drop_index(op.f('ix_trajectory_decisions_session_id'), table_name='trajectory_decisions')
    op.drop_index('ix_trajdec_session', table_name='trajectory_decisions')
    op.drop_index('ix_trajdec_app_time', table_name='trajectory_decisions')
    op.drop_table('trajectory_decisions')
    op.drop_table('system_settings')
    op.drop_table('scenario_configs')
    op.drop_index('ix_routing_out_fp', table_name='routing_outcomes')
    op.drop_index('ix_routing_out_created', table_name='routing_outcomes')
    op.drop_index('ix_routing_out_app', table_name='routing_outcomes')
    op.drop_table('routing_outcomes')
    op.drop_table('rbac_roles')
    op.drop_index('ix_pricing_model_lookup', table_name='pricing_models')
    op.drop_table('pricing_models')
    op.drop_table('notification_configs')
    op.drop_index('ix_merkle_time', table_name='merkle_roots')
    op.drop_table('merkle_roots')
    op.drop_table('hash_chain_state')
    op.drop_index('ix_fed_sync_synced', table_name='federation_sync_log')
    op.drop_table('federation_sync_log')
    op.drop_table('federation_merged_results')
    op.drop_table('federation_consent')
    op.drop_index('ix_cot_team_seq', table_name='cot_ledger_entries')
    op.drop_index('ix_cot_team_created', table_name='cot_ledger_entries')
    op.drop_index('ix_cot_linked_proposal', table_name='cot_ledger_entries')
    op.drop_index('ix_cot_decision_type', table_name='cot_ledger_entries')
    op.drop_table('cot_ledger_entries')
    op.drop_table('cost_centers')
    op.drop_table('consortium_peers')
    op.drop_table('billing_connections')
    op.drop_index('ix_billing_actuals_provider_period', table_name='billing_actuals')
    op.drop_table('billing_actuals')
    op.drop_index('ix_audit_team', table_name='audit_log')
    op.drop_index('ix_audit_resource', table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_entry_hash'), table_name='audit_log')
    op.drop_index('ix_audit_actor', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index('ix_audit_checkpoint_seq', table_name='audit_checkpoints')
    op.drop_table('audit_checkpoints')
    op.drop_index('ix_attrnode_session', table_name='attribution_nodes')
    op.drop_index('ix_attrnode_app_amp', table_name='attribution_nodes')
    op.drop_index(op.f('ix_attribution_nodes_session_id'), table_name='attribution_nodes')
    op.drop_table('attribution_nodes')
