import type { TemplateReference } from '@/features/editor/sceneApi';
import type { ScenePage } from '@/features/editor/sceneTypes';

export type TemplateSuggestion = { page: ScenePage; reference: TemplateReference; role: string };

function intendedRole(page: ScenePage, index: number, count: number): string {
  const explicit = page.outline_content?.role;
  if (explicit && explicit !== 'unknown') return explicit;
  const title = page.outline_content?.title || '';
  if (/(目录|议程|contents|agenda)/i.test(title)) return 'agenda';
  if (count > 1 && index === 0) return 'cover';
  if (count > 1 && index === count - 1) return 'closing';
  if (/(章节|部分|section)/i.test(title)) return 'section';
  return 'content';
}

export function suggestTemplateMatches(pages: ScenePage[], references: TemplateReference[]): TemplateSuggestion[] {
  const analyzed = references.filter(reference => reference.analysis_status === 'completed' &&
    typeof reference.analysis?.role === 'string');
  const useCount = new Map<string, number>();
  const suggestions: TemplateSuggestion[] = [];
  pages.forEach((page, index) => {
    if (page.template_asset_id) return; // Never replace an explicit manual choice.
    const role = intendedRole(page, index, pages.length);
    const exact = analyzed.filter(reference => reference.analysis?.role === role);
    // A content reference is a safe fallback for agenda/section/closing, but
    // never use a cover or closing example as a generic body page.
    const pool = exact.length ? exact : analyzed.filter(reference => reference.analysis?.role === 'content');
    if (!pool.length) return;
    const reference = pool.reduce((best, candidate) =>
      (useCount.get(candidate.template_asset_id) || 0) < (useCount.get(best.template_asset_id) || 0)
        ? candidate : best);
    useCount.set(reference.template_asset_id, (useCount.get(reference.template_asset_id) || 0) + 1);
    suggestions.push({ page, reference, role });
  });
  return suggestions;
}
