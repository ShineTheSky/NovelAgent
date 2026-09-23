import { useState } from 'react';
import type { PendingQuestion, QuestionAnswer } from '../types/chat';

export function QuestionDialog({ qa }: { qa: PendingQuestion }) {
  const [answers, setAnswers] = useState<Record<number, QuestionAnswer>>({});
  const [customAnswers, setCustomAnswers] = useState<Record<number, string>>({});

  const setAnswer = (index: number, value: QuestionAnswer) => {
    setAnswers(previous => ({ ...previous, [index]: value }));
  };

  const answerFor = (index: number): QuestionAnswer => {
    const custom = customAnswers[index]?.trim() ?? '';
    const selected = answers[index];
    if (!custom) return selected ?? '';
    const choices = Array.isArray(selected) ? selected : selected ? [selected] : [];
    return choices.length > 0 ? [...choices, custom] : custom;
  };

  const handleSubmit = () => {
    qa.onSubmit(qa.questions.map((_, index) => answerFor(index)));
  };

  const allAnswered = qa.questions.every((_, index) => {
    const answer = answerFor(index);
    return answer !== undefined && answer !== '' && (!Array.isArray(answer) || answer.length > 0);
  });

  return (
    <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center z-50" role="dialog" aria-modal="true" aria-labelledby="question-dialog-title">
      <div className="bg-white rounded-2xl shadow-2xl p-6 max-w-md w-full mx-4 max-h-[80vh] overflow-y-auto">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-full bg-purple-100 flex items-center justify-center text-lg" aria-hidden="true">?</div>
          <div>
            <h3 id="question-dialog-title" className="font-semibold text-gray-800">需要确认</h3>
            <p className="text-xs text-gray-400">Agent 需要你做出选择</p>
          </div>
        </div>

        {qa.questions.map((question, index) => (
          <fieldset key={`${question.header}-${index}`} className="mb-4 last:mb-0">
            <legend className="flex items-center gap-2 mb-2">
              <span className="text-xs font-mono bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded">{question.header}</span>
              <span className="text-sm text-gray-700">{question.question}</span>
            </legend>
            <div className="space-y-1.5">
              {question.options.map(option => {
                const current = answers[index];
                const isSelected = question.multiSelect
                  ? Array.isArray(current) && current.includes(option.label)
                  : current === option.label;

                return (
                  <label key={option.label} className={`flex items-start gap-2 p-2 rounded-lg border cursor-pointer transition-colors ${isSelected ? 'border-purple-400 bg-purple-50' : 'border-gray-200 hover:bg-gray-50'}`}>
                    <input
                      type="checkbox"
                      name={`question-${index}`}
                      className="mt-0.5 accent-purple-600"
                      checked={isSelected}
                      onChange={() => {
                        if (!question.multiSelect) {
                          setAnswer(index, isSelected ? '' : option.label);
                          return;
                        }
                        const selected = Array.isArray(current) ? [...current] : [];
                        setAnswer(index, isSelected ? selected.filter(label => label !== option.label) : [...selected, option.label]);
                      }}
                    />
                    <span>
                      <span className="block text-sm font-medium text-gray-700">{option.label}</span>
                      {option.description && <span className="block text-xs text-gray-400">{option.description}</span>}
                    </span>
                  </label>
                );
              })}
            </div>
            <label className="mt-2.5 block">
              <span className="mb-1 block text-xs text-gray-500">补充意见或直接输入回答</span>
              <textarea
                value={customAnswers[index] ?? ''}
                onChange={event => setCustomAnswers(previous => ({ ...previous, [index]: event.target.value }))}
                rows={2}
                placeholder="可单独填写，也可补充说明已选项…"
                className="w-full resize-y rounded-lg border border-gray-200 bg-white px-2.5 py-2 text-sm leading-5 text-gray-700 outline-none placeholder:text-gray-400 focus:border-purple-400"
              />
            </label>
          </fieldset>
        ))}

        <button type="button" onClick={handleSubmit} disabled={!allAnswered} className={`w-full mt-4 py-2.5 text-sm rounded-xl font-medium transition-colors ${allAnswered ? 'bg-purple-600 text-white hover:bg-purple-700' : 'bg-gray-200 text-gray-400 cursor-not-allowed'}`}>
          提交
        </button>
      </div>
    </div>
  );
}
