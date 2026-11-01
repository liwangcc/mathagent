// Read both current and legacy heatmap payloads without displaying error types.
export function knowledgeTopicStudents(topic) {
  const names = Array.isArray(topic?.students)
    ? topic.students
    : (Array.isArray(topic?.errors) ? topic.errors : []).flatMap(
      group => Array.isArray(group?.students) ? group.students : [],
    )
  return [...new Set(names.filter(name => typeof name === 'string' && name.trim()).map(name => name.trim()))]
}
