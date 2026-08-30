import { Navigate, Route, Routes } from 'react-router-dom';

import { AppLayout } from '../components/layout/AppLayout';
import { ClarificationPage } from './ClarificationPage';
import { DiscoveryPage } from './DiscoveryPage';
import { ExceptionReviewPage } from './ExceptionReviewPage';
import { JobDetailPage } from './JobDetailPage';
import { PlanConfirmationPage } from './PlanConfirmationPage';
import { PlanReviewPage } from './PlanReviewPage';
import { ResultDashboardPage } from './ResultDashboardPage';
import { SettingsPage } from './SettingsPage';
import { UploadPage } from './UploadPage';

export function AppRoutes() {
  return (
    <AppLayout>
      <Routes>
        <Route path="/" element={<Navigate to="/upload" replace />} />
        <Route path="/upload" element={<UploadPage />} />
        <Route path="/jobs/:jobId" element={<JobDetailPage />} />
        <Route path="/jobs/:jobId/discovery" element={<DiscoveryPage />} />
        <Route path="/jobs/:jobId/plan" element={<PlanReviewPage />} />
        <Route path="/jobs/:jobId/result" element={<ResultDashboardPage />} />
        <Route path="/jobs/:jobId/review" element={<ExceptionReviewPage />} />
        <Route path="/jobs/:jobId/clarification" element={<ClarificationPage />} />
        <Route path="/jobs/:jobId/plan-confirmation" element={<PlanConfirmationPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Routes>
    </AppLayout>
  );
}
