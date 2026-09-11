export interface NovelDocument {
  id: string;
  type: 'volume_outline' | 'chapter_outline' | 'section';
  volume: number;
  chapter: number | null;
  section: number | null;
  title: string;
  path: string;
  char_count: number;
  updated_at: string;
}

export interface NovelChapter {
  volume: number;
  chapter: number;
  title: string;
  outline: NovelDocument | null;
  sections: NovelDocument[];
  char_count: number;
}

export interface NovelVolume {
  volume: number;
  title: string;
  outline: NovelDocument | null;
  chapters: NovelChapter[];
  char_count: number;
}

export interface NovelTree {
  project_id: string;
  volumes: NovelVolume[];
}
