ALTER TABLE agent.cards
    DROP CONSTRAINT agent_cards_repository_scope_check,
    DROP COLUMN repository_scope;
