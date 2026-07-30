DO $agent_worker_base_branch_rollback$
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
            'Unsupported database for Agent worker grant rollback: %',
            current_database();
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = worker_role_name
    ) THEN
        EXECUTE format(
            'REVOKE UPDATE (base_branch) ON agent.cards FROM %I',
            worker_role_name
        );
    END IF;
END;
$agent_worker_base_branch_rollback$;
