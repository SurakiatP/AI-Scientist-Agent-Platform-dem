export type Language = 'th' | 'en';

const messages = {
  en: {
    skip: 'Skip to content', brand: 'AI Scientist Agent Platform', navLabel: 'Main navigation',
    home: 'Home', projects: 'Projects', sources: 'Sources & outputs', history: 'Run history', settings: 'Settings',
    openNavigation: 'Open navigation', closeNavigation: 'Close navigation', navDialog: 'Navigation',
    landingEyebrow: 'A workspace for careful scientific exploration', heroTitle: 'Turn a question into a clearer view of the evidence.',
    heroText: 'Explore scientific questions, follow the sources, and keep each finding connected to the work that produced it.',
    getStarted: 'Get started', exploreLab: 'Explore the lab',
    workflowTitle: 'A research process you can follow', workflowIntro: 'Move from a focused question to evidence you can inspect and a synthesis you can trace.',
    stepQuestion: '01 · Frame a question', stepQuestionText: 'Choose a project and make the question and scope clear before work begins.',
    stepEvidence: '02 · Examine evidence', stepEvidenceText: 'Review sources and see what each one can support.',
    stepSynthesis: '03 · Connect the findings', stepSynthesisText: 'Bring evidence together while keeping its context and limits in view.',
    labTitle: 'See diffusion in motion', labIntro: 'Change the diffusion setting and observe a simple particle model.', labName: 'Diffusion lab',
    workflowEyebrow: 'A clear path through complex questions', labEyebrow: 'A small interactive model', footerLine: 'Research begins with a question.',
    illustrative: 'Illustrative model · not calibrated or validated for scientific prediction', diffusion: 'Diffusion', play: 'Play', pause: 'Pause', reset: 'Reset',
    readout: 'Diffusion setting: {value}', particleCaption: 'Particles spread through a shared space as the setting changes.',
    appearanceTitle: 'Appearance', appearanceDescription: 'Choose how the platform looks on this device.',
    light: 'Light', dark: 'Dark', system: 'System', language: 'Language', languageDescription: 'Choose the language for navigation and controls.',
    english: 'English', thai: 'ไทย', workspaceTitle: 'Projects', workspaceUnavailable: 'Project and research data appear here when the local service is connected.',
    sourcesTitle: 'Sources & outputs', historyTitle: 'Run history', settingsTitle: 'Settings',
    settingsUnavailable: 'More workspace settings will be available here.',
  },
  th: {
    skip: 'ข้ามไปยังเนื้อหา', brand: 'AI Scientist Agent Platform', navLabel: 'เมนูหลัก',
    home: 'หน้าแรก', projects: 'โปรเจกต์', sources: 'แหล่งอ้างอิงและผลงาน', history: 'ประวัติงาน', settings: 'ตั้งค่า',
    openNavigation: 'เปิดเมนูนำทาง', closeNavigation: 'ปิดเมนูนำทาง', navDialog: 'เมนูนำทาง',
    landingEyebrow: 'พื้นที่ทำงานสำหรับการสำรวจทางวิทยาศาสตร์อย่างรอบคอบ', heroTitle: 'เปลี่ยนคำถามให้เห็นหลักฐานได้ชัดเจนขึ้น',
    heroText: 'สำรวจคำถามทางวิทยาศาสตร์ ตรวจสอบแหล่งที่มา และเชื่อมโยงข้อค้นพบกับงานที่ทำให้เกิดข้อค้นพบนั้น',
    getStarted: 'เริ่มต้นใช้งาน', exploreLab: 'ทดลองในห้องปฏิบัติการ',
    workflowTitle: 'ขั้นตอนการวิจัยที่ติดตามได้', workflowIntro: 'เริ่มจากคำถามที่ชัดเจน ไปสู่หลักฐานที่ตรวจสอบและข้อสังเคราะห์ที่ตามรอยได้',
    stepQuestion: '01 · ตั้งคำถาม', stepQuestionText: 'เลือกโครงการและกำหนดคำถามกับขอบเขตให้ชัดเจนก่อนเริ่มงาน',
    stepEvidence: '02 · ตรวจสอบหลักฐาน', stepEvidenceText: 'ทบทวนแหล่งข้อมูลและพิจารณาว่าแต่ละแหล่งสนับสนุนข้อสรุปใดได้บ้าง',
    stepSynthesis: '03 · เชื่อมโยงข้อค้นพบ', stepSynthesisText: 'สังเคราะห์หลักฐานโดยคงบริบทและข้อจำกัดไว้ให้เห็น',
    labTitle: 'สังเกตการแพร่กระจาย', labIntro: 'ปรับค่าการแพร่และสังเกตแบบจำลองอนุภาคอย่างง่าย', labName: 'ห้องทดลองการแพร่',
    workflowEyebrow: 'เส้นทางชัดเจนสำหรับคำถามที่ซับซ้อน', labEyebrow: 'แบบจำลองเชิงโต้ตอบอย่างง่าย', footerLine: 'การวิจัยเริ่มต้นจากคำถาม',
    illustrative: 'แบบจำลองเพื่อประกอบความเข้าใจ · ยังไม่ได้ปรับเทียบหรือยืนยันเพื่อการพยากรณ์ทางวิทยาศาสตร์', diffusion: 'การแพร่', play: 'เล่น', pause: 'หยุดชั่วคราว', reset: 'เริ่มใหม่',
    readout: 'ค่าการแพร่: {value}', particleCaption: 'อนุภาคกระจายทั่วพื้นที่เมื่อเปลี่ยนค่าการแพร่',
    appearanceTitle: 'การแสดงผล', appearanceDescription: 'เลือกบรรยากาศที่อ่านสบายสำหรับงานของคุณ',
    light: 'สว่าง', dark: 'มืด', system: 'ตามระบบ', language: 'ภาษา', languageDescription: 'เลือกภาษาสำหรับเมนูและตัวควบคุม',
    english: 'English', thai: 'ไทย', workspaceTitle: 'โครงการ', workspaceUnavailable: 'ข้อมูลโครงการและงานวิจัยจะแสดงที่นี่เมื่อเชื่อมต่อบริการในเครื่องแล้ว',
    sourcesTitle: 'แหล่งข้อมูลและผลงาน', historyTitle: 'ประวัติการทำงาน', settingsTitle: 'ตั้งค่า',
    settingsUnavailable: 'การตั้งค่าเพิ่มเติมของพื้นที่ทำงานจะแสดงที่นี่',
  },
} satisfies Record<Language, Record<string, string>>;

export type MessageKey = keyof typeof messages.en;
export function t(key: MessageKey, language: Language): string {
  return messages[language][key] ?? messages.en[key];
}
