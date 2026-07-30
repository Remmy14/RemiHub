DO $agent_worker_card_grants$
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
            'Unsupported database for Agent worker card grants: %',
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

    EXECUTE format(
        'GRANT UPDATE (repository_scope, base_branch) ON agent.cards TO %I',
        worker_role_name
    );
END;
$agent_worker_card_grants$;
