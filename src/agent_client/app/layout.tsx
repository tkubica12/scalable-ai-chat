import '@copilotkit/react-ui/styles.css';
import './styles.css';

export const metadata = {
  title: 'Scalable AI Chat Agent Client',
  description: 'CopilotKit migration client for the durable run/event API'
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
