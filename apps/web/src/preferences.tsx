import { useLayoutEffect, useState } from 'react';
import type { Language } from './locales';
import { t } from './locales';

export type Appearance = 'light' | 'dark' | 'system';
const LANGUAGE_KEY = 'scientist-platform.language';
const APPEARANCE_KEY = 'scientist-platform.appearance';

function readLanguage(): Language {
  return localStorage.getItem(LANGUAGE_KEY) === 'th' ? 'th' : 'en';
}
function readAppearance(): Appearance {
  const value = localStorage.getItem(APPEARANCE_KEY);
  return value === 'light' || value === 'dark' || value === 'system' ? value : 'system';
}

export function usePreferences() {
  const [language, setLanguageState] = useState<Language>(readLanguage);
  const [appearance, setAppearanceState] = useState<Appearance>(readAppearance);

  const setLanguage = (value: Language) => {
    localStorage.setItem(LANGUAGE_KEY, value);
    setLanguageState(value);
  };
  const setAppearance = (value: Appearance) => {
    localStorage.setItem(APPEARANCE_KEY, value);
    setAppearanceState(value);
  };

  useLayoutEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => {
      document.documentElement.dataset.theme = appearance === 'system' ? (media.matches ? 'dark' : 'light') : appearance;
      document.documentElement.lang = language;
    };
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, [appearance, language]);

  return { language, appearance, setLanguage, setAppearance };
}

export function AppearanceSettings({ language, appearance, setLanguage, setAppearance }: ReturnType<typeof usePreferences>) {
  return <section className="settings-panel original-appearance" aria-labelledby="appearance-title">
    <div className="settings-group">
      <h2 id="appearance-title">{t('appearanceTitle', language)}</h2>
      <p>{t('appearanceDescription', language)}</p>
      <fieldset className="choice-group mode-grid">
        <legend className="visually-hidden">{t('appearanceTitle', language)}</legend>
        {(['light', 'dark', 'system'] as const).map((value) => <label className="choice mode-card" key={value}>
          <span className={`mode-sample mode-sample-${value}`} aria-hidden="true"><i /><b /><em /></span>
          <span className="mode-card-title"><input aria-label={t(value, language)} type="radio" name="appearance" value={value} checked={appearance === value} onChange={() => setAppearance(value)} />{t(value, language)}</span>
          <small>{language === 'th' ? value === 'light' ? 'พื้นสว่าง อ่านเอกสารชัดเจน' : value === 'dark' ? 'พื้นเข้ม เหมาะกับการใช้งานตอนกลางคืน' : 'ปรับตามธีมของเครื่องโดยอัตโนมัติ' : value === 'light' ? 'A clear canvas for reading.' : value === 'dark' ? 'A comfortable canvas for evening work.' : 'Automatically follow your device theme.'}</small>
        </label>)}
      </fieldset>
    </div>
    <div className="settings-group">
      <h2>{t('language', language)}</h2>
      <p>{t('languageDescription', language)}</p>
      <fieldset className="choice-group">
        <legend className="visually-hidden">{t('language', language)}</legend>
        {(['en', 'th'] as const).map((value) => <label className="choice" key={value}>
          <input type="radio" name="language-setting" value={value} checked={language === value} onChange={() => setLanguage(value)} />
          <span>{t(value === 'en' ? 'english' : 'thai', language)}</span>
        </label>)}
      </fieldset>
    </div>
  </section>;
}
