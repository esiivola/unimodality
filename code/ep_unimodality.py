import numpy as np
import time
from scipy.integrate import quad, dblquad
from scipy.stats import norm
from scipy.misc import logsumexp

import GPy
from GPy.inference.latent_function_inference.expectation_propagation import posteriorParams, marginalMoments, gaussianApproximation, cavityParams
from GPy.inference.latent_function_inference.posterior import PosteriorEP as Posterior

from GPy.util.linalg import  dtrtrs, dpotrs, tdot, symmetrify, jitchol

from probit_moments import ProbitMoments
from moment_functions import compute_moments_softinformation, compute_moments_strict
from util import mult_diag

npdf = lambda x, m, v: 1./np.sqrt(2*np.pi*v)*np.exp(-(x-m)**2/(2*v))
log_npdf = lambda x, m, v: -0.5*np.log(2*np.pi*v) -(x-m)**2/(2*v)
phi = lambda x: norm.cdf(x)
logphi = lambda x: norm.logcdf(x)

log_2_pi = np.log(2*np.pi)

def update_posterior(K, eta, theta):
    D = K.shape[0]
    sqrt_theta = np.sqrt(theta)
    G = sqrt_theta[:, None]*K
    B = np.identity(D) + G*sqrt_theta
    L = jitchol(B)
    V = np.linalg.solve(L, G)
    Sigma_full = K - np.dot(V.T, V)
    mu = np.dot(Sigma_full, eta)
    #Sigma = np.diag(Sigma_full)

    return posteriorParams(mu=mu, Sigma=Sigma_full, L=L)

    # return mu, Sigma, Sigma_full, L

def ep_unimodality(X1, X2, t, y, Kf_kernel, Kg_kernel_list, sigma2, t2=None, m=None, max_itt=50, nu=10., nu2 = 1., alpha=0.9, tol=1e-6, verbose=0, moment_function=None, seed=0):

    np.random.seed(seed)
    t0 = time.time()

    if t2 is None:
        t2 = t.copy()


    N, D = t.shape
    M = len(t2)
    Df = N + D*M
    Dg = M + M

    if m is None:
        m = np.ones((D, M))

    # moment function
    if moment_function is None:
        moment_function = compute_moments_strict


    ###################################################################################
    # Contruct kernels
    ###################################################################################
    Kf = Kf_kernel.K(X1)
    Kg_list = [kg.K(X2) for kg in Kg_kernel_list]


    ###################################################################################
    # Prepare marginal moments, site approximation and cavity containers
    ###################################################################################

    # for f
    f_marg_moments = marginalMoments(Df) 
    f_ga_approx = gaussianApproximation(v=np.zeros(Df), tau=np.zeros(Df))
    f_cavity = cavityParams(Df) 

    # insert likelihood information
    f_ga_approx.v[:N] = y[:, 0]/sigma2
    f_ga_approx.tau[:N] = 1./sigma2

    # for each g
    g_marg_moments_list = [marginalMoments(2*M) for d in range(D)]
    g_ga_approx_list = [gaussianApproximation(v=np.zeros(2*M), tau=np.zeros(2*M)) for d in range(D)]
    g_cavity_list = [cavityParams(2*M) for d in range(D)]

    # hardcode eta to 1
    eta = 1

    ###################################################################################
    # Prepare global approximations
    ###################################################################################
    f_posterior = update_posterior(Kf, f_ga_approx.v, f_ga_approx.tau)
    g_posterior_list = [update_posterior(Kg_list[d], g_ga_approx_list[d].v, g_ga_approx_list[d].tau) for d in range(D)]



    ###################################################################################
    # Iterate
    ###################################################################################
    for itt in range(max_itt):

        old_params = np.hstack((f_posterior.mu, f_posterior.Sigma_diag)) # , mu_g, Sigma_g

        if verbose > 0:
            print('Iteration %d' % (itt + 1))

        # approximate constraints to enforce monotonicity to g
        d_list = np.random.choice(range(D), size=D, replace=False)
        for d in d_list:

            # get relevant EP parameters for dimension d
            g_posterior = g_posterior_list[d]
            g_ga_approx = g_ga_approx_list[d]
            g_cavity = g_cavity_list[d]
            g_marg_mom = g_marg_moments_list[d]

            j_list = np.random.choice(range(M), size=M, replace=False) if M > 0 else []
            for j in j_list:

                # compute offset for radient indices
                i = M + j

                # update cavity
                g_cavity._update_i(eta=eta, ga_approx=g_ga_approx, post_params=g_posterior, i=i)

                # match moments
                try:
                    g_marg_mom.Z_hat[i], g_marg_mom.mu_hat[i], g_marg_mom.sigma2_hat[i] = match_moments_g(m[d,j], g_cavity.v[i], g_cavity.tau[i], nu)
                except AssertionError:
                    print('Numerical problem g-term i = %d, j = %d for dim = %d in iteration %d. Skipping update' % (i, j, d, itt))
                    continue

                # update
                g_ga_approx._update_i(eta=eta, delta=alpha, post_params=g_posterior, marg_moments=g_marg_mom, i=i)


            # update joint
            g_posterior_list[d] = update_posterior(Kg_list[d], g_ga_approx.v, g_ga_approx.tau)

      # approximate constraints to enforce a single sign change for f'
        d_list = np.random.choice(range(D), size=D, replace=False)
        for d in d_list:

            # get relevant EP parameters for dimension d
            g_posterior = g_posterior_list[d]
            g_ga_approx = g_ga_approx_list[d]
            g_cavity = g_cavity_list[d]
            g_marg_mom = g_marg_moments_list[d]

            j_list = np.random.choice(range(M), size=M, replace=False) if M > 0 else []
            for j in j_list:

                i = N + d*M +  j

                # update cavities for f & g
                f_cavity._update_i(eta=eta, ga_approx=f_ga_approx, post_params=f_posterior, i=i)
                g_cavity._update_i(eta=eta, ga_approx=g_ga_approx, post_params=g_posterior, i=j)

                # match moments
                try:
                    mom_f, mom_g = match_moments_fg(f_cavity.v[i], f_cavity.tau[i], g_cavity.v[j], g_cavity.tau[j], nu2, moment_function)
                except AssertionError:
                    print('Numerical problem fg-term i = %d, j = %d for dim = %d in iteration %d. Skipping update' % (i, j, d, itt))
                    continue

                # update marginal moments
                f_marg_moments.Z_hat[i], f_marg_moments.mu_hat[i], f_marg_moments.sigma2_hat[i] = mom_f
                g_marg_mom.Z_hat[j], g_marg_mom.mu_hat[j], g_marg_mom.sigma2_hat[j] = mom_g

                # update sites
                f_ga_approx._update_i(eta=eta, delta=alpha, post_params=f_posterior, marg_moments=f_marg_moments, i=i)
                g_ga_approx._update_i(eta=eta, delta=alpha, post_params=g_posterior, marg_moments=g_marg_mom, i=j)

            # update posterior
            g_posterior_list[d] = update_posterior(Kg_list[d], g_ga_approx.v, g_ga_approx.tau)
            f_posterior = update_posterior(Kf, f_ga_approx.v, f_ga_approx.tau)

      # check for convergence
        new_params = np.hstack((f_posterior.mu, f_posterior.Sigma_diag)) # , mu_g, Sigma_g
        if len(old_params) > 0 and np.mean((new_params-old_params)**2)/np.mean(old_params**2) < tol:
            run_time = time.time() - t0

            if verbose > 0:
                print('Converged in %d iterations in %4.3fs' % (itt + 1, run_time))
            break

    #############################################################################3
    # Marginal likelihood & gradients
    #############################################################################3

    # compute normalization constant for likelihoods
    for i in range(N):
        f_cavity._update_i(eta=eta, ga_approx=f_ga_approx, post_params=f_posterior, i=i)
        f_marg_moments.Z_hat[i] = npdf(y[i, 0], f_cavity.v[i]/f_cavity.tau[i], 1./f_cavity.tau[i] + sigma2)


    # marginal likelihood and gradient contribution from f
    Z_tilde = _log_Z_tilde(f_marg_moments, f_ga_approx, f_cavity)
    f_post, f_logZ, f_grad = _inference(Kf, f_ga_approx, f_cavity, None, Z_tilde)
    grad_dict = {'dL_dK_f': f_grad['dL_dK']}

    # marginal likelihood and gradient contribution from each g
    g_logZs = []
    g_grads = []
    for d in range(D):
        Z_tilde = _log_Z_tilde(g_marg_moments_list[d], g_ga_approx_list[d], g_cavity_list[d])
        g_post, g_logZ, g_grad = _inference(Kg_list[d], g_ga_approx_list[d], g_cavity_list[d], None, Z_tilde)

        g_logZs.append(g_logZ)
        g_grads.append(g_grad)

    
    for d in range(D):
        grad_dict['dL_dK_g%d' % d] = g_grads[d]['dL_dK']

    # sum contributions
    logZ = f_logZ + np.sum(g_logZs)


    # Done
    return f_posterior, g_posterior_list, Kf, logZ, grad_dict#, mu_g, Sigma_g, Sigma_full_g, logZ

def compute_dl_dK(posterior, K, eta, theta, prior_mean = 0):
    tau, v = theta, eta

    tau_tilde_root = np.sqrt(tau)
    Sroot_tilde_K = tau_tilde_root[:,None] * K
    aux_alpha , _ = dpotrs(posterior.L, np.dot(Sroot_tilde_K, v), lower=1)
    alpha = (v - tau_tilde_root * aux_alpha)[:,None] #(K + Sigma^(\tilde))^(-1) /mu^(/tilde)
    LWi, _ = dtrtrs(posterior.L, np.diag(tau_tilde_root), lower=1)
    Wi = np.dot(LWi.T, LWi)
    symmetrify(Wi) #(K + Sigma^(\tilde))^(-1)

    dL_dK = 0.5 * (tdot(alpha) - Wi)
    
    return dL_dK


def compute_marginal_likelihood_mvn(posterior, eta, theta, skip_problematic=None):

    mu, Sigma = posterior.mu, posterior.Sigma
    
    b = np.linalg.solve(posterior.L, eta/np.sqrt(theta))

    # skip problematic term that will cancel out later?
    if skip_problematic is None:
        problematic_term = - np.sum(np.log(np.sqrt(theta)))
    elif skip_problematic == 0:
        problematic_term = 0
    elif skip_problematic > 0:
        problematic_term = - np.sum(np.log(np.sqrt(theta[:skip_problematic])))

    logdet = np.sum(np.log(np.diag(posterior.L))) + problematic_term
    quadterm = 0.5*np.sum(b**2)

    return -0.5*len(mu)*np.log(2*np.pi)  - logdet - quadterm


def match_moments_g(m, eta_cav, theta_cav, nu):

    # compute mean and variance of cavity
    m_cav, v_cav = eta_cav/theta_cav, 1./theta_cav

    # compute moments
    Z, site_m, site_m2 = ProbitMoments.compute_moments(m=0, v=1./(m*nu), mu=m_cav, sigma2=v_cav, return_normalizer=True, normalized=True)
    
    # compute variance
    site_v = site_m2 - site_m**2

    return Z, site_m, site_v


def match_moments_fg(eta_cav_fp, theta_cav_fp, eta_cav_g, theta_cav_g, nu2, moment_function):

    # transform to means and variances
    m_cav_fp, v_cav_fp = eta_cav_fp/theta_cav_fp, 1./theta_cav_fp
    m_cav_g, v_cav_g = eta_cav_g/theta_cav_g, 1./theta_cav_g

    # compute moments
    Z, site_fp_m, site_fp_m2, site_g_m, site_g_m2 = moment_function(m_cav_fp, v_cav_fp, m_cav_g, v_cav_g, nu2=nu2)

    # variances
    site_fp_v = site_fp_m2 - site_fp_m**2
    site_g_v = site_g_m2 - site_g_m**2

    return (Z, site_fp_m, site_fp_v), (1, site_g_m, site_g_v)

def _log_Z_tilde(marg_moments, ga_approx, cav_params):
    return np.sum((np.log(marg_moments.Z_hat) + 0.5*np.log(2*np.pi) + 0.5*np.log(1+ga_approx.tau/cav_params.tau) - 0.5 * ((ga_approx.v)**2 * 1./(cav_params.tau + ga_approx.tau))
            + 0.5*(cav_params.v * ( ( (ga_approx.tau/cav_params.tau) * cav_params.v - 2.0 * ga_approx.v ) * 1./(cav_params.tau + ga_approx.tau)))))


def _ep_marginal(K, ga_approx, Z_tilde):
    post_params = posteriorParams._recompute(K, ga_approx)

    # Gaussian log marginal excluding terms that can go to infinity due to arbitrarily small tau_tilde.
    # These terms cancel out with the terms excluded from Z_tilde
    B_logdet = np.sum(2.0*np.log(np.diag(post_params.L)))
    log_marginal =  0.5*(-len(ga_approx.tau) * log_2_pi - B_logdet + np.sum(ga_approx.v * np.dot(post_params.Sigma,ga_approx.v)))
    log_marginal += Z_tilde

    return log_marginal, post_params



def _inference(K, ga_approx, cav_params, likelihood, Z_tilde, Y_metadata=None):
    log_marginal, post_params = _ep_marginal(K, ga_approx, Z_tilde)

    tau_tilde_root = np.sqrt(ga_approx.tau)
    Sroot_tilde_K = tau_tilde_root[:,None] * K

    aux_alpha , _ = dpotrs(post_params.L, np.dot(Sroot_tilde_K, ga_approx.v), lower=1)
    alpha = (ga_approx.v - tau_tilde_root * aux_alpha)[:,None] #(K + Sigma^(\tilde))^(-1) /mu^(/tilde)
    LWi, _ = dtrtrs(post_params.L, np.diag(tau_tilde_root), lower=1)
    Wi = np.dot(LWi.T,LWi)
    symmetrify(Wi) #(K + Sigma^(\tilde))^(-1)

    dL_dK = 0.5 * (tdot(alpha) - Wi)
    dL_dthetaL = 0 #likelihood.ep_gradients(Y, cav_params.tau, cav_params.v, np.diag(dL_dK), Y_metadata=Y_metadata, quad_mode='gh')
    #temp2 = likelihood.ep_gradients(Y, cav_params.tau, cav_params.v, np.diag(dL_dK), Y_metadata=Y_metadata, quad_mode='naive')
    #temp = likelihood.exact_inference_gradients(np.diag(dL_dK), Y_metadata = Y_metadata)
    #print("exact: {}, approx: {}, Ztilde: {}, naive: {}".format(temp, dL_dthetaL, Z_tilde, temp2))
    return Posterior(woodbury_inv=Wi, woodbury_vector=alpha, K=K), log_marginal, {'dL_dK':dL_dK, 'dL_dthetaL':dL_dthetaL, 'dL_dm':alpha}
