import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { AnimatePresence, MotionConfig } from 'framer-motion';
import { AuthProvider } from './context/AuthContext';
import { GrainOverlay } from './components/ui';
import Header from './Header';
import Home from './Home';
import Editor from './Editor';
import Guideline from './Guideline';
import Errors from './Errors';
import MySchemas from './MySchemas';
import Profile from './Profile';
import Login from './Login';
import Register from './Register';
import ProtectedRoute from './ProtectedRoute';
import InviteAccept from './InviteAccept';
import SharedDiagram from './SharedDiagram';
import NotFound from './NotFound';
import './index.css';

function App() {
    return (
        <BrowserRouter>
            <AuthProvider>
                <MotionConfig reducedMotion="user">
                    <Header />
                    <GrainOverlay />
                    <AnimatePresence mode="wait">
                        <Routes>
                            <Route path="/" element={<Home />} />
                            <Route path="/login" element={<Login />} />
                            <Route path="/reset-password" element={<Login />} />
                            <Route path="/register" element={<Register />} />
                            <Route path="/share/:token" element={<SharedDiagram />} />
                            <Route element={<ProtectedRoute />}>
                                <Route path="/editor" element={<Editor />} />
                                <Route path="/guideline" element={<Guideline />} />
                                <Route path="/errors" element={<Errors />} />
                                <Route path="/my-schemas" element={<MySchemas />} />
                                <Route path="/profile" element={<Profile />} />
                                <Route path="/invite/:token" element={<InviteAccept />} />
                            </Route>
                            <Route path="*" element={<NotFound />} />
                        </Routes>
                    </AnimatePresence>
                </MotionConfig>
            </AuthProvider>
        </BrowserRouter>
    );
}

export default App;
