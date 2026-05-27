import RaceScreen from './RaceScreen';
import DraftCompanionScreen from "./DraftCompanionScreen";

function App() {
  const path = window.location.pathname;

  if (path.startsWith("/race/draft")) {
    return <DraftCompanionScreen />;
  }

  return <RaceScreen />;
}

export default App;