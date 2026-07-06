import RaceScreen from "./RaceScreen";
import DraftCompanionScreen from "./DraftCompanionScreen";
import RhStorageStatusScreen from "./RhStorageStatusScreen";

function App() {
  const path = window.location.pathname;

  if (path.startsWith("/race/draft")) {
    return <DraftCompanionScreen />;
  }

  if (path.startsWith("/storage")) {
    return <RhStorageStatusScreen />;
  }

  return <RaceScreen />;
}

export default App;