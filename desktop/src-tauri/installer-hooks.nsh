!macro NSIS_HOOK_POSTINSTALL
  ${If} $NoShortcutMode <> 1
    Call CreateOrUpdateDesktopShortcut

    ; Updates normally preserve a deleted shortcut. Restore it for MathAgent's automatic shortcut policy.
    ${IfNot} ${FileExists} "$DESKTOP\${PRODUCTNAME}.lnk"
      CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
      !insertmacro SetLnkAppUserModelId "$DESKTOP\${PRODUCTNAME}.lnk"
    ${EndIf}
  ${EndIf}
!macroend
