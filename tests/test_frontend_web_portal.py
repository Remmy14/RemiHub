import json
import unittest
from pathlib import Path


class FrontendWebPortalTests(unittest.TestCase):
    def test_firebase_browser_config_is_source_controlled_and_complete(self):
        config_path = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "firebase-browser"
            / "firebaseBrowserConfig.json"
        )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        missing = [
            key
            for key in (
                "apiKey",
                "authDomain",
                "projectId",
                "storageBucket",
                "messagingSenderId",
                "appId",
            )
            if not isinstance(config.get(key), str) or not config[key].strip()
        ]

        self.assertEqual(missing, [])
        self.assertEqual(config["projectId"], "remihub-33a90")

    def test_frontend_uses_same_origin_public_race_api(self):
        frontend_root = Path(__file__).resolve().parents[1] / "frontend-web"

        for relative_path in (
            "src/RaceScreen.tsx",
            "src/DraftCompanionScreen.tsx",
        ):
            with self.subTest(path=relative_path):
                source = (frontend_root / relative_path).read_text(
                    encoding="utf-8",
                )
                self.assertNotIn("https://remillard.duckdns.org", source)

    def test_frontend_has_authenticated_api_helper_without_legacy_key(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "authenticatedApi.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("Authorization", source)
        self.assertIn("Bearer", source)
        self.assertNotIn("X-RemiHub-Key", source)

    def test_frontend_uses_firebase_javascript_sdk(self):
        auth_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "auth"
            / "firebaseAuth.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("initializeApp", auth_source)
        self.assertIn("getAuth", auth_source)
        self.assertIn("onAuthStateChanged", auth_source)
        self.assertIn("signInWithEmailAndPassword", auth_source)
        self.assertIn("getIdToken", auth_source)
        self.assertNotIn("identitytoolkit.googleapis.com", auth_source)
        self.assertNotIn("refresh_token", auth_source)


    def test_frontend_authentication_is_sign_in_only(self):
        frontend_root = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
        )

        auth_source = (
            frontend_root
            / "src"
            / "auth"
            / "firebaseAuth.ts"
        ).read_text(encoding="utf-8")

        app_source = (
            frontend_root
            / "src"
            / "App.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "signInWithEmailAndPassword",
            auth_source,
        )

        for forbidden_api in (
            "createUserWithEmailAndPassword",
            "deleteUser(",
        ):
            with self.subTest(forbidden_api=forbidden_api):
                self.assertNotIn(
                    forbidden_api,
                    auth_source,
                )

        for forbidden_ui in (
            "Sign up",
            "Create account",
            "Register account",
        ):
            with self.subTest(forbidden_ui=forbidden_ui):
                self.assertNotIn(
                    forbidden_ui,
                    app_source,
                )

    def test_agent_portal_route_stays_inside_authenticated_shell(self):
        app_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "App.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('import AgentScreen from "./AgentScreen";', app_source)
        self.assertIn('path.startsWith("/agent")', app_source)
        self.assertIn("<AuthenticatedRoute>", app_source)
        self.assertLess(
            app_source.index('path.startsWith("/race")'),
            app_source.index("<AuthenticatedRoute>"),
        )
        self.assertGreater(
            app_source.index('path.startsWith("/agent")'),
            app_source.index("function PrivateApp()"),
        )

    def test_health_portal_route_stays_inside_authenticated_shell(self):
        app_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "App.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('import HealthScreen from "./HealthScreen";', app_source)
        self.assertIn('path.startsWith("/health")', app_source)
        self.assertIn("<AuthenticatedRoute>", app_source)
        self.assertIn("<HealthScreen />", app_source)
        self.assertLess(
            app_source.index('path.startsWith("/race")'),
            app_source.index("<AuthenticatedRoute>"),
        )
        self.assertGreater(
            app_source.index('path.startsWith("/health")'),
            app_source.index("function PrivateApp()"),
        )

    def test_agent_frontend_uses_authenticated_api_only(self):
        agent_api_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "agentApi.ts"
        ).read_text(encoding="utf-8")

        self.assertIn('from "./authenticatedApi"', agent_api_source)
        self.assertIn("apiRequest<", agent_api_source)
        self.assertNotIn("authenticated: false", agent_api_source)
        self.assertNotIn("X-RemiHub-Key", agent_api_source)
        self.assertNotIn("api_key", agent_api_source)
        self.assertNotIn("token=", agent_api_source)

    def test_health_frontend_uses_authenticated_services_api_only(self):
        frontend_root = Path(__file__).resolve().parents[1] / "frontend-web"
        health_api_source = (
            frontend_root
            / "src"
            / "api"
            / "healthApi.ts"
        ).read_text(encoding="utf-8")
        health_screen_source = (
            frontend_root
            / "src"
            / "HealthScreen.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('from "./authenticatedApi"', health_api_source)
        self.assertIn("apiRequest<ServiceHealthSnapshotResponse>", health_api_source)
        self.assertIn('"/health/services"', health_api_source)
        self.assertNotIn("authenticated: false", health_api_source)
        self.assertNotIn("X-RemiHub-Key", health_api_source)
        self.assertNotIn("api_key", health_api_source)
        self.assertNotIn("token=", health_api_source)
        self.assertNotIn("Authorization", health_screen_source)
        self.assertNotIn("Bearer", health_screen_source)
        self.assertNotIn("/health/systemd", health_api_source)

    def test_health_frontend_models_match_deployed_contract_shape(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "healthApi.ts"
        ).read_text(encoding="utf-8")

        for status in (
            '"healthy"',
            '"degraded"',
            '"unhealthy"',
            '"idle"',
            '"unknown"',
        ):
            with self.subTest(status=status):
                self.assertIn(status, source)

        for field in (
            "checked_at",
            "overall",
            "components",
            "group",
            "kind",
            "status",
            "message",
            "expected_mode",
            "systemd",
            "dependencies",
            "active_state",
            "sub_state",
            "result",
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)

    def test_health_screen_presents_statuses_groups_and_stale_refresh(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "HealthScreen.tsx"
        ).read_text(encoding="utf-8")

        for status_label in (
            "Healthy",
            "Idle",
            "Degraded",
            "Unhealthy",
            "Unknown",
            "Normal standby",
            "Not confirmed healthy",
        ):
            with self.subTest(status_label=status_label):
                self.assertIn(status_label, source)

        for group in (
            '"core"',
            '"agent"',
            '"storage"',
            '"rh_storage"',
            '"media"',
        ):
            with self.subTest(group=group):
                self.assertIn(group, source)

        self.assertIn("groupedComponents", source)
        self.assertIn("knownGroupOrder", source)
        self.assertIn("Stale snapshot", source)
        self.assertIn("Last refresh failed", source)
        self.assertIn("requestInFlight", source)
        self.assertIn("POLL_INTERVAL_MILLIS = 30000", source)
        self.assertIn("document.visibilityState", source)
        self.assertIn("No health components were returned", source)
        self.assertIn("Component details", source)

    def test_fitness_frontend_has_one_completed_workout_detail_path(self):
        frontend_root = Path(__file__).resolve().parents[1] / "frontend-web"
        screen_source = (frontend_root / "src" / "FitnessScreen.tsx").read_text(
            encoding="utf-8",
        )
        api_source = (frontend_root / "src" / "api" / "fitnessApi.ts").read_text(
            encoding="utf-8",
        )

        self.assertEqual(screen_source.count("function CompletedWorkoutDetailDialog"), 1)
        self.assertIn("const [detailWorkout, setDetailWorkout]", screen_source)
        self.assertIn("const openCompletedDetail = async", screen_source)
        self.assertIn("onOpenCompletedDetail={(workout) => void openCompletedDetail(workout)}", screen_source)
        self.assertIn("workout.status !== \"COMPLETED\"", screen_source)
        self.assertIn("workout.status === \"COMPLETED\"", screen_source)
        self.assertIn("getScheduledWorkout(workout.id)", screen_source)
        self.assertIn("resultSourceLabel(workout)", screen_source)
        self.assertIn("liftingEntryLabel", screen_source)
        self.assertIn("No recorded workout details are available.", screen_source)
        self.assertNotIn(
            "This completed workout does not have external result metrics.",
            screen_source,
        )
        self.assertIn("listCompletedWorkoutsForTemplate", api_source)
        self.assertIn("/completed-workouts", api_source)

    def test_fitness_frontend_models_optional_result_metrics(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "fitnessApi.ts"
        ).read_text(encoding="utf-8")

        for field in (
            "external_provider: string | null",
            "moving_duration_seconds: number | null",
            "average_speed_meters_per_second: number | null",
            "average_hr: number | null",
            "max_hr: number | null",
            "training_load: number | null",
            "aerobic_training_effect: number | null",
            "anaerobic_training_effect: number | null",
            "training_effect_label: string | null",
            "hr_zone_1_seconds: number | null",
            "average_cadence_spm: number | null",
            "average_power_watts: number | null",
            "average_stride_length_meters: number | null",
            "elevation_gain_meters: number | null",
            "calories: number | null",
            "steps: number | null",
            "vo2_max: number | null",
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)

    def test_fitness_frontend_models_lifting_results_as_aggregate_entries(self):
        api_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "fitnessApi.ts"
        ).read_text(encoding="utf-8")
        screen_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "FitnessScreen.tsx"
        ).read_text(encoding="utf-8")

        for field in (
            "export type FitnessLiftingEntry",
            "exercise_name: string",
            "weight: number",
            "reps: number",
            "sets: number | null",
            "notes: string | null",
            "lifting_result: FitnessLiftingResult | null",
        ):
            with self.subTest(field=field):
                self.assertIn(field, api_source)

        self.assertIn("function liftingEntryLabel", screen_source)
        self.assertIn("sets\"} x ${reps} @ ${weight}", screen_source)
        self.assertNotIn("Set 1", screen_source)

    def test_fitness_frontend_exposes_template_and_plan_history_by_id(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "FitnessScreen.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("function TemplateHistoricalDialog", source)
        self.assertIn("listCompletedWorkoutsForTemplate(template.id)", source)
        self.assertIn("Historical", source)
        self.assertIn("function PlansView({", source)
        self.assertIn("getPlanInstance(instance.id)", source)
        self.assertIn("scheduled_workouts", source)
        self.assertIn("Open detail", source)

    def test_fitness_frontend_defaults_to_dashboard_landing(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "FitnessScreen.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('type FitnessTab = "dashboard"', source)
        self.assertIn('{ id: "dashboard", label: "Dashboard", href: "/portal/fitness" }', source)
        self.assertIn('return "dashboard";', source)
        self.assertIn('activeTab === "dashboard"', source)
        self.assertIn("function DashboardView({", source)
        self.assertNotIn('activeTab === "today"', source)
        self.assertNotIn("function TodayView", source)

        for tab in (
            '{ id: "schedule", label: "Schedule", href: "/portal/fitness/schedule" }',
            '{ id: "calendar", label: "Calendar", href: "/portal/fitness/calendar" }',
            'label: "Workout Templates"',
            '{ id: "plans", label: "Training Plans", href: "/portal/fitness/plans" }',
            '{ id: "weightlifting", label: "Weightlifting", href: "/portal/fitness/weightlifting" }',
        ):
            with self.subTest(tab=tab):
                self.assertIn(tab, source)

    def test_fitness_dashboard_composes_existing_durable_apis(self):
        frontend_root = Path(__file__).resolve().parents[1] / "frontend-web"
        screen_source = (frontend_root / "src" / "FitnessScreen.tsx").read_text(
            encoding="utf-8",
        )
        api_source = (frontend_root / "src" / "api" / "fitnessApi.ts").read_text(
            encoding="utf-8",
        )

        for api_fragment in (
            "getTrainingCalendar(weekStart, weekEnd)",
            "listScheduledWorkouts(today, futureEnd)",
            "listWorkoutHistory(historyStart, today)",
            "getCurrentPlanInstance()",
            "getPlanInstance(activePlan.id)",
            "getWorkoutTemplate(upcomingRun.workout_template_id)",
            "listCompletedWorkoutsForTemplate(upcomingRun.workout_template_id)",
        ):
            with self.subTest(api_fragment=api_fragment):
                self.assertIn(api_fragment, screen_source)

        self.assertIn("/fitness/history?", api_source)
        self.assertIn("/fitness/plan-instances/current", api_source)
        self.assertIn("workout.status === \"PLANNED\"", screen_source)
        self.assertIn('plannedFutureWorkouts.find((workout) => workout.type === "RUNNING")', screen_source)
        self.assertNotIn("listCompletedWorkoutsForTemplate(upcomingRun.workout_name", screen_source)
        self.assertNotIn("listCompletedWorkoutsForTemplate(nextRun.workout_name", screen_source)

    def test_fitness_dashboard_renders_required_sections_and_summaries(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "FitnessScreen.tsx"
        ).read_text(encoding="utf-8")

        for section in (
            "This Week",
            "Up Next",
            "Next Run",
            "Previous attempts",
            "Current Training Plan",
            "Recent Activity",
        ):
            with self.subTest(section=section):
                self.assertIn(section, source)

        for calculation in (
            'workout.status !== "RESCHEDULED"',
            'workout.type === "RUNNING"',
            'workout.type === "LIFTING"',
            'workout.status === "COMPLETED"',
            "completed_distance_miles",
            "planned_distance_miles",
            "completedPlanWorkouts.length",
            "remainingPlanWorkouts.length",
            "completedWorkoutSummary(workout)",
            "No planned workouts are scheduled.",
            "No planned running workouts are scheduled.",
            "No prior completed attempts for this template.",
            "No active training plan.",
            "No completed workouts found recently.",
        ):
            with self.subTest(calculation=calculation):
                self.assertIn(calculation, source)

    def test_fitness_dashboard_completed_items_use_shared_detail_path(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "FitnessScreen.tsx"
        ).read_text(encoding="utf-8")

        self.assertEqual(source.count("function CompletedWorkoutDetailDialog"), 1)
        self.assertIn("function DashboardWorkoutLink({", source)
        self.assertIn("onClick={() => onOpenCompletedDetail(workout)}", source)
        self.assertIn("getScheduledWorkout(workout.id)", source)
        self.assertIn("onOpenCompletedDetail={(workout) => void openCompletedDetail(workout)}", source)
        self.assertNotIn("function DashboardCompletedWorkoutDetail", source)
        self.assertNotIn("function GarminDashboard", source)
        self.assertNotIn("function WeightliftingDashboardResultDialog", source)

    def test_health_screen_does_not_duplicate_systemd_health_semantics(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "HealthScreen.tsx"
        ).read_text(encoding="utf-8")

        forbidden_fragments = (
            'active_state === "inactive"',
            'active_state === "active"',
            'sub_state === "running"',
            'sub_state === "waiting"',
            "systemctl",
            "journalctl",
            "restartService",
            "startService",
            "stopService",
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

    def test_agent_frontend_does_not_invent_lifecycle_routes(self):
        agent_api_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "agentApi.ts"
        ).read_text(encoding="utf-8")

        expected_routes = (
            "/agent/cards",
            "/approve-implementation",
            "/approve-deployment",
            "/retry",
            "/retry-github-sync",
            "/cancel",
            "/close",
            "/messages",
        )
        for route in expected_routes:
            with self.subTest(route=route):
                self.assertIn(route, agent_api_source)

        self.assertNotIn("review-stage", agent_api_source)
        self.assertNotIn("review_stage", agent_api_source)

    def test_agent_backend_router_remains_admin_only(self):
        router_source = (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "routers"
            / "agent.py"
        ).read_text(encoding="utf-8")

        self.assertIn("require_admin_principal", router_source)
        self.assertIn("dependencies=[Depends(require_admin_principal)]", router_source)
        self.assertNotIn("get_current_principal", router_source)

    def test_race_public_exception_is_not_broadened_for_agent(self):
        main_source = (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "main.py"
        ).read_text(encoding="utf-8")

        self.assertIn("app.include_router(agent.router)", main_source)
        self.assertIn("app.include_router(race.router)", main_source)
        self.assertIn("protected_routers = [", main_source)
        self.assertLess(
            main_source.index("app.include_router(agent.router)"),
            main_source.index("protected_routers = ["),
        )
        self.assertLess(
            main_source.index("app.include_router(race.router)"),
            main_source.index("protected_routers = ["),
        )


if __name__ == "__main__":
    unittest.main()
