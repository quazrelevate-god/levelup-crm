import { createBrowserRouter, Navigate } from 'react-router-dom'

import { AiCallingRulesPage } from '@/routes/AiCallingRulesPage'
import { AppLayout } from '@/routes/AppLayout'
import { AppsPage } from '@/routes/AppsPage'
import { CustomActionsPage } from '@/routes/CustomActionsPage'
import { DashboardPage } from '@/routes/DashboardPage'
import { DashboardSettingsPage } from '@/routes/DashboardSettingsPage'
import { EditReportPage } from '@/routes/EditReportPage'
import { FieldSettingsPage } from '@/routes/FieldSettingsPage'
import { ImportPage } from '@/routes/ImportPage'
import { IntegrationsPage } from '@/routes/IntegrationsPage'
import { CallDetailPage } from '@/routes/CallDetailPage'
import { CallsPage } from '@/routes/CallsPage'
import { LeadsPage } from '@/routes/LeadsPage'
import { ForgotPasswordPage } from '@/routes/ForgotPasswordPage'
import { LoginPage } from '@/routes/LoginPage'
import { SetPasswordPage } from '@/routes/SetPasswordPage'
import { MembersPage } from '@/routes/MembersPage'
import { AssignmentSettingsPage } from '@/routes/AssignmentSettingsPage'
import { PermissionsPage } from '@/routes/PermissionsPage'
import { PipelineSettingsPage } from '@/routes/PipelineSettingsPage'
import { RazorpayAppPage } from '@/routes/RazorpayAppPage'
import { RequireAuth } from '@/routes/RequireAuth'
import { ScheduledReportsPage } from '@/routes/ScheduledReportsPage'
import { SettingsLayout } from '@/routes/SettingsLayout'
import { SettingsOverviewPage } from '@/routes/SettingsOverviewPage'
import { SystemStatusPage } from '@/routes/SystemStatusPage'
import { TasksPage } from '@/routes/TasksPage'
import { TemplatesPage } from '@/routes/TemplatesPage'
import { VoiceExtractionSettingsPage } from '@/routes/VoiceExtractionSettingsPage'
import { WalletPage } from '@/routes/WalletPage'
import { CallingNumbersPage } from '@/routes/CallingNumbersPage'
import { CallFactsPage } from '@/routes/CallFactsPage'
import { VoiceVariablesPage } from '@/routes/VoiceVariablesPage'
import { WorkflowEditorPage } from '@/routes/WorkflowEditorPage'
import { WorkflowsPage } from '@/routes/WorkflowsPage'
import { WorkspacePickerPage } from '@/routes/WorkspacePickerPage'
import { OperatorLayout } from '@/features/operator/OperatorLayout'
import { CompanyPage } from '@/routes/operator/CompanyPage'
import { NewCompanyPage } from '@/routes/operator/NewCompanyPage'
import { OperatorHomePage } from '@/routes/operator/OperatorHomePage'
import {
  NumberRequestsPage,
  OperatorHistoryPage,
  OperatorLoginsPage,
} from '@/routes/operator/OperatorMiscPages'

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  // Unauthenticated by design — this is where every account begins.
  { path: '/set-password', element: <SetPasswordPage /> },
  { path: '/forgot-password', element: <ForgotPasswordPage /> },
  {
    // The operator console (docs/19): above every company, operator only.
    path: '/operator',
    element: <OperatorLayout />,
    children: [
      { index: true, element: <OperatorHomePage /> },
      { path: 'companies/new', element: <NewCompanyPage /> },
      { path: 'companies/:companyId', element: <CompanyPage /> },
      { path: 'numbers', element: <NumberRequestsPage /> },
      { path: 'history', element: <OperatorHistoryPage /> },
      { path: 'logins', element: <OperatorLoginsPage /> },
    ],
  },
  {
    // Signed in, but no workspace chosen yet — the picker sits between the two
    // guards, so it must not require an active workspace itself.
    path: '/workspaces',
    element: (
      <RequireAuth>
        <WorkspacePickerPage />
      </RequireAuth>
    ),
  },
  {
    path: '/',
    element: <AppLayout />,
    children: [
      // Leads is the landing page now that there is something to land on.
      { index: true, element: <Navigate to="/leads" replace /> },
      { path: 'dashboard', element: <DashboardPage /> },
      { path: 'leads', element: <LeadsPage /> },
      { path: 'calls', element: <CallsPage /> },
      { path: 'calls/:callId', element: <CallDetailPage /> },
      { path: 'tasks', element: <TasksPage /> },
      { path: 'import', element: <ImportPage /> },
      { path: 'edit-report', element: <EditReportPage /> },
      { path: 'templates', element: <TemplatesPage /> },
      { path: 'members', element: <MembersPage /> },
      { path: 'wallet', element: <WalletPage /> },
      { path: 'automations', element: <WorkflowsPage /> },
      { path: 'automations/:workflowId', element: <WorkflowEditorPage /> },
      { path: 'apps', element: <AppsPage /> },
      { path: 'apps/razorpay', element: <RazorpayAppPage /> },
      // The deployment status page moved under Settings with the rest of the
      // operator surface. The old path still works so bookmarks and the
      // existing test suite do not break on a redesign.
      { path: 'status', element: <Navigate to="/settings/system" replace /> },
      {
        // One door. Everything that configures the workspace, plus the
        // deployment's own internals, lives behind it.
        path: 'settings',
        element: <SettingsLayout />,
        children: [
          { index: true, element: <SettingsOverviewPage /> },
          { path: 'fields', element: <FieldSettingsPage /> },
          { path: 'pipeline', element: <PipelineSettingsPage /> },
          { path: 'custom-actions', element: <CustomActionsPage /> },
          { path: 'permissions', element: <PermissionsPage /> },
          { path: 'assignment', element: <AssignmentSettingsPage /> },
          { path: 'scheduled-reports', element: <ScheduledReportsPage /> },
          { path: 'dashboards', element: <DashboardSettingsPage /> },
          { path: 'integrations', element: <IntegrationsPage /> },
          { path: 'voice-extraction', element: <VoiceExtractionSettingsPage /> },
          { path: 'voice-variables', element: <VoiceVariablesPage /> },
          { path: 'calling-numbers', element: <CallingNumbersPage /> },
          { path: 'call-facts', element: <CallFactsPage /> },
          { path: 'ai-calling-rules', element: <AiCallingRulesPage /> },
          // Moved to the main sidebar; old links still land on it.
          { path: 'wallet', element: <Navigate to="/wallet" replace /> },
          { path: 'system', element: <SystemStatusPage /> },
        ],
      },
    ],
  },
])
