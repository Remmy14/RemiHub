DO $agent_worker_events_select_grant$
DECLARE
    worker_role_name text;
BEGIN
    IF session_user ~ '_migrator$' THEN
        worker_role_name := regexp_replace(
            session_user,
            '_migrator$',
            '_agent_worker'
        );
    ELSIF current_database() = 'remihub' THEN
        worker_role_name := 'remihub_agent_worker';
    ELSIF current_database() = 'remihub_qa' THEN
        worker_role_name := 'remihub_qa_agent_worker';
    ELSE
        RAISE EXCEPTION
            'Unsupported database for Agent worker event-read grant: %',
            current_database();
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = worker_role_name
    ) THEN
        RAISE EXCEPTION
            'Required Agent worker role is missing: %',
            worker_role_name;
    END IF;

    IF to_regclass('agent.events') IS NULL THEN
        RAISE EXCEPTION 'Required table is missing: agent.events';
    END IF;

    EXECUTE format(
        'GRANT SELECT ON agent.events TO %I',
        worker_role_name
    );
END;
$agent_worker_events_select_grant$;
