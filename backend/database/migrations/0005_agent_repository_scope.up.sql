ALTER TABLE agent.cards
    ADD COLUMN repository_scope text DEFAULT 'backend' NOT NULL;

ALTER TABLE agent.cards
    ALTER COLUMN repository_scope SET DEFAULT 'auto',
    ADD CONSTRAINT agent_cards_repository_scope_check
        CHECK (
            repository_scope IN (
                'auto',
                'backend',
                'android',
                'backend_and_android'
            )
        );
