ALTER TABLE agent.backlog_items ADD COLUMN sort_order bigint;

CREATE INDEX agent_backlog_custom_order_idx
    ON agent.backlog_items (sort_order, id) WHERE promoted_card_id IS NULL;

CREATE TABLE agent.backlog_order_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL DEFAULT 0,
    initialized boolean NOT NULL DEFAULT false
);
