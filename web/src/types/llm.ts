export interface LLMModelOption {
  model: string;
  label: string;
}

export interface LLMProviderOption {
  key: string;
  name: string;
  base_url: string;
  is_configured: boolean;
  has_temporary_key: boolean;
  models: LLMModelOption[];
}

export interface LLMPositionSetting {
  label: string;
  provider: string;
  model: string;
}

export interface LLMSettings {
  positions: Record<string, LLMPositionSetting>;
  providers: LLMProviderOption[];
}

export type LLMPositionUpdate = Pick<LLMPositionSetting, 'provider' | 'model'>;

export interface ProviderSettingsUpdate {
  base_url: string;
  api_key?: string;
  storage: 'persistent' | 'temporary';
}
