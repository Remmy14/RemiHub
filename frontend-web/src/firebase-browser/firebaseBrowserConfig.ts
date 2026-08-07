import rawConfig from "./firebaseBrowserConfig.json";

export type FirebaseBrowserConfig = {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket: string;
  messagingSenderId: string;
  appId: string;
};

const requiredKeys = [
  "apiKey",
  "authDomain",
  "projectId",
  "storageBucket",
  "messagingSenderId",
  "appId",
] as const;

export function validateFirebaseBrowserConfig(
  config: Partial<FirebaseBrowserConfig>,
): FirebaseBrowserConfig {
  const missing = requiredKeys.filter((key) => {
    const value = config[key];
    return typeof value !== "string" || value.trim().length === 0;
  });

  if (missing.length > 0) {
    throw new Error(
      `Missing Firebase browser configuration keys: ${missing.join(", ")}`,
    );
  }

  return {
    apiKey: config.apiKey!.trim(),
    authDomain: config.authDomain!.trim(),
    projectId: config.projectId!.trim(),
    storageBucket: config.storageBucket!.trim(),
    messagingSenderId: config.messagingSenderId!.trim(),
    appId: config.appId!.trim(),
  };
}

export const firebaseBrowserConfig =
  validateFirebaseBrowserConfig(rawConfig);
