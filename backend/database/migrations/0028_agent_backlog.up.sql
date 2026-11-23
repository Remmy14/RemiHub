CREATE TABLE agent.backlog_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    prompt text NOT NULL,
    priority text NOT NULL DEFAULT 'Medium',
    created_by uuid NOT NULL REFERENCES public.remihub_users(id) ON DELETE RESTRICT,
    promoted_card_id uuid UNIQUE REFERENCES agent.cards(id) ON DELETE RESTRICT,
    promoted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_backlog_title_check CHECK (char_length(title) <= 160 AND char_length(btrim(title)) > 0),
    CONSTRAINT agent_backlog_prompt_check CHECK (char_length(prompt) <= 20000 AND char_length(btrim(prompt)) > 0),
    CONSTRAINT agent_backlog_priority_check CHECK (priority IN ('High', 'Medium', 'Low')),
    CONSTRAINT agent_backlog_promotion_check CHECK ((promoted_card_id IS NULL) = (promoted_at IS NULL))
);

CREATE INDEX agent_backlog_active_order_idx
    ON agent.backlog_items (created_at DESC, id DESC)
    WHERE promoted_card_id IS NULL;
