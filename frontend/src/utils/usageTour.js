// First-use walkthrough: every step points at a real control (a `data-tour`
// anchor) on the page a teacher actually uses, in grading-workflow order.
// `targets` lists anchors by preference; later entries are fallbacks for page
// states where the preferred control is not rendered.

function onPaths(prefixes) {
  return (path) => prefixes.some((prefix) => (
    prefix.endsWith('/') ? path.startsWith(prefix) : path === prefix || path.startsWith(`${prefix}/`)
  ))
}

const onClassPage = onPaths(['/students', '/class/'])
const onUploadPage = onPaths(['/upload'])

const LATER_STAGE_NOTE = '当前批次已进入后续步骤；开始新的一批时，这里会出现对应按钮。'

export const SERVICE_STATE_LEGEND = [
  { label: '可用', type: 'success', text: '可以立即开始新的批阅。' },
  { label: '需排队', type: 'warning', text: '可以提交，系统排队处理；不要重复提交同一批。' },
  { label: '暂不可用', type: 'danger', text: '暂时禁止开启新任务，避免产生不完整结果；已有记录照常查看。' },
]

function classSteps(classes, currentClassId) {
  const currentClass = classes.find((item) => item.id === currentClassId) || null
  const studentCount = Number(currentClass?.student_count || 0)
  const switcher = currentClass
    ? {
        title: '先确认当前班级',
        body: `现在是「${currentClass.class_name}」。要批改别的班，点这里切换；批改结果会记到当前班级。`,
      }
    : classes.length
      ? { title: '先选一个班级', body: '点这里选择要批改的班级；批改结果会记到当前班级。' }
      : { title: '这里显示当前班级', body: '批改结果会记到这里显示的班级。你还没有班级，下一步带你去创建。' }

  let roster
  if (!classes.length) {
    roster = {
      targets: ['class-create'],
      title: '点「添加班级」创建班级',
      body: '填好班级名称并保存，再点「添加学生」录入名单。',
    }
  } else if (currentClass && studentCount === 0) {
    roster = {
      targets: ['student-add'],
      title: '点「添加学生」录入名单',
      body: '姓名和学号要与学生写在试卷上的一致，系统才能把每份试卷对到人。',
    }
  } else {
    roster = {
      targets: ['student-add'],
      title: '名单有变化，点这里补充',
      body: currentClass
        ? `当前班级有 ${studentCount} 名学生。新学期或有转入转出时，先核对姓名和学号再批改。`
        : '新学期或有转入转出时，先核对姓名和学号再批改。',
    }
  }

  return [
    { key: 'class-switcher', targets: ['class-switcher'], ...switcher },
    { key: 'roster', route: '/students', isOn: onClassPage, ...roster },
  ]
}

export function buildUsageTourSteps({ classes = [], currentClassId = null } = {}) {
  const classList = Array.isArray(classes) ? classes : []
  const upload = { route: '/upload', isOn: onUploadPage }
  return [
    ...classSteps(classList, currentClassId),
    {
      key: 'grade-category',
      ...upload,
      targets: ['grade-category', 'grade-workflow'],
      fallbackNote: LATER_STAGE_NOTE,
      title: '批改前，先选批次类型',
      body: '日常作业只统计对错；考试会在确认模板时填写分值。下方「试卷栏数」默认双栏，按实际试卷选择。',
    },
    {
      key: 'template-import',
      ...upload,
      targets: ['template-import', 'grade-workflow'],
      fallbackNote: LATER_STAGE_NOTE,
      title: '点这里导入答案模板',
      body: '试卷有几页就传几张，再点「识别模板」。核对题目和标准答案后保存，保存后的答案才是本次批改依据。',
    },
    {
      key: 'paper-upload',
      ...upload,
      targets: ['paper-upload', 'grade-workflow'],
      fallbackNote: LATER_STAGE_NOTE,
      title: '把学生试卷放进来',
      body: '点「选择文件」或「选择文件夹」，也可以直接拖到这里；支持图片和 ZIP / RAR。请学生在每页顶部写姓名和学号。',
    },
    {
      key: 'service-state',
      ...upload,
      targets: ['service-state', 'grade-workflow'],
      fallbackNote: LATER_STAGE_NOTE,
      title: '看这个标识，再决定开始',
      body: '标识只表示能否开启新任务，不代表已提交任务的进度。',
      legend: SERVICE_STATE_LEGEND,
    },
    {
      key: 'start-grading',
      ...upload,
      targets: ['start-grading', 'grade-workflow'],
      fallbackNote: LATER_STAGE_NOTE,
      title: '材料齐了，点「开始批阅」',
      body: '模板保存、试卷选好后才能点。系统先识别，你核对识别结果和学生身份后再批改；处理中会显示已完成、失败和剩余数量。',
    },
    {
      key: 'review',
      ...upload,
      targets: ['grade-workflow'],
      title: '批改完，就在这一页复核归档',
      body: '这一行显示你走到了哪一步。批改完成后，先处理待复核题和身份不确定的试卷，对照原卷确认后归档。',
      emphasis: '身份、答案、评分和正式成绩均以教师确认为准。',
    },
    {
      key: 'replay',
      targets: ['guide-button'],
      title: '忘了在哪点，就点这里',
      body: '随时点「使用引导」重看。归档后的结果在左侧「批改记录」「成绩报告」「统计分析」中查看。',
    },
  ]
}
