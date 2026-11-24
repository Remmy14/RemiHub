DROP TABLE agent.backlog_order_state;
-- Dropping sort_order also drops its dependent index.
ALTER TABLE agent.backlog_items DROP COLUMN sort_order;
