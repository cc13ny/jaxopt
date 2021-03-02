# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from absl.testing import absltest
from absl.testing import parameterized

import jax
from jax import test_util as jtu
import jax.numpy as jnp

from jaxopt import fista
from jaxopt import loss
from jaxopt import prox

from sklearn import datasets
from sklearn import preprocessing
from sklearn import linear_model


def _lasso_skl(X, y, lam, tol=1e-5):
  X = preprocessing.Normalizer().fit_transform(X)
  lasso = linear_model.Lasso(fit_intercept=False, alpha=lam, tol=tol)
  return lasso.fit(X, y).coef_


def _make_lasso_objective(X, y):
  X = preprocessing.Normalizer().fit_transform(X)
  def fun_f(w, lam):
    y_pred = jnp.dot(X, w)
    diff = y_pred - y
    return 0.5 / (lam * X.shape[0]) * jnp.dot(diff, diff)
  return fun_f


def _logreg_skl(X, y, lam, tol=1e-5):
  X = preprocessing.Normalizer().fit_transform(X)
  logreg = linear_model.LogisticRegression(fit_intercept=False, C=1. / lam,
                                           multi_class="multinomial", tol=tol)
  return logreg.fit(X, y).coef_.T


def _make_logreg_objective(X, y):
  X = preprocessing.Normalizer().fit_transform(X)
  def fun_f(W, lam):
    logits = jnp.dot(X, W)
    return (jnp.sum(jax.vmap(loss.multiclass_logistic_loss)(y, logits)) +
            0.5 * lam * jnp.sum(W ** 2))
  return fun_f


class FISTATest(jtu.JaxTestCase):

  @parameterized.product(acceleration=[True, False],
                         implicit_diff=[True, False])
  def test_lasso(self, acceleration, implicit_diff):
    X, y = datasets.load_boston(return_X_y=True)
    lam = 1.0
    tol = 1e-3 if acceleration else 5e-3
    max_iter = 200
    atol = 1e-2 if acceleration else 1e-1
    fun_f = _make_lasso_objective(X, y)

    # Check optimality conditions.
    w_init = jnp.zeros(X.shape[1])
    w_fit = fista.fista(fun_f, w_init, params_f=lam, prox_g=prox.prox_l1,
                        tol=tol, max_iter=max_iter, acceleration=acceleration,
                        implicit_diff=implicit_diff, verbose=0)
    w_fit2 = prox.prox_l1(w_fit - jax.grad(fun_f)(w_fit, lam))
    self.assertLessEqual(jnp.sqrt(jnp.sum((w_fit - w_fit2) ** 2)), tol)

    # Compare against sklearn.
    w_skl = _lasso_skl(X, y, lam)
    self.assertArraysAllClose(w_fit, w_skl, atol=atol)

  def test_lasso_implicit_diff(self):
    X, y = datasets.load_boston(return_X_y=True)
    lam = 1.0
    eps = 1e-5
    fun_f = _make_lasso_objective(X, y)

    # Jacobian w.r.t. lam using finite central finite difference.
    # We use the sklearn solver for precision, as it operates on float64.
    jac_lam = (_lasso_skl(X, y, lam + eps) -
               _lasso_skl(X, y, lam - eps)) / (2 * eps)

    # Compute the Jacobian w.r.t. lam via implicit differentiation.
    w_skl = _lasso_skl(X, y, lam)
    I = jnp.eye(len(w_skl))
    fun = lambda g: fista._implicit_diff_prox_vjp(w_skl, fun_f, lam,
                                                  prox.prox_l1, g)
    jac_lam2 = jax.vmap(fun)(I)
    self.assertArraysAllClose(jac_lam, jac_lam2, atol=1e-3)

    # Make sure the custom VJP works.
    w_init = jnp.zeros(X.shape[1])
    tol = 1e-3
    max_iter = 200
    jac_fun = jax.jacrev(fista.fista, argnums=2)
    jac_lam3 = jac_fun(fun_f, w_init, lam, prox_g=prox.prox_l1,
                       tol=tol, max_iter=max_iter, acceleration=True,
                       implicit_diff=True)
    self.assertArraysAllClose(jac_lam, jac_lam3, atol=1e-2)

  @parameterized.product(acceleration=[True, False])
  def test_lasso_forward_diff(self, acceleration):
    raise absltest.SkipTest
    X, y = datasets.load_boston(return_X_y=True)
    lam = 1.0
    tol = 1e-4 if acceleration else 5e-3
    max_iter = 200
    eps = 1e-5
    fun_f = _make_lasso_objective(X, y)

    jac_lam = (_lasso_skl(X, y, lam + eps) -
               _lasso_skl(X, y, lam - eps)) / (2 * eps)

    # Compute the Jacobian w.r.t. lam via forward differentiation.
    w_init = jnp.zeros(X.shape[1])
    jac_fun = jax.jacfwd(fista.fista, argnums=2)
    jac_lam2 = jac_fun(fun_f, w_init, lam, prox_g=prox.prox_l1, tol=tol,
                       max_iter=max_iter, implicit_diff=False,
                       acceleration=acceleration)
    self.assertArraysAllClose(jac_lam, jac_lam2, atol=1e-3)

  def test_logreg(self, acceleration=True):
    X, y = datasets.load_digits(return_X_y=True)
    lam = float(X.shape[0])
    tol = 1e-3 if acceleration else 5e-3
    max_iter = 200
    atol = 1e-3 if acceleration else 1e-1
    fun_f = _make_logreg_objective(X, y)

    W_init = jnp.zeros((X.shape[1], 10))
    W_fit = fista.fista(fun_f, W_init, params_f=lam, prox_g=None, tol=tol,
                        acceleration=acceleration)

    # Check optimality conditions.
    W_grad = jax.grad(fun_f)(W_fit, lam)
    self.assertLessEqual(jnp.sqrt(jnp.sum(W_grad ** 2)), tol)

    # Compare against sklearn.
    W_skl = _logreg_skl(X, y, lam)
    self.assertArraysAllClose(W_fit, W_skl, atol=atol)

  def test_logreg_implicit_diff(self):
    X, y = datasets.load_digits(return_X_y=True)
    lam = float(X.shape[0])
    eps = 1e-5
    fun_f = _make_logreg_objective(X, y)

    # Jacobian w.r.t. lam using finite central finite difference.
    # We use the sklearn solver for precision, as it operates on float64.
    jac_lam = (_logreg_skl(X, y, lam + eps) -
               _logreg_skl(X, y, lam - eps)) / (2 * eps)

    # Compute the Jacobian w.r.t. lam via implicit differentiation.
    W_skl = _logreg_skl(X, y, lam)
    I = jnp.eye(W_skl.size)
    I = I.reshape(-1, *W_skl.shape)
    fun = lambda g: fista._implicit_diff_prox_vjp(W_skl, fun_f, lam, None, g)
    jac_lam2 = jax.vmap(fun)(I).reshape(*W_skl.shape)
    self.assertArraysAllClose(jac_lam, jac_lam2, atol=1e-3)

    # Make sure the custom VJP works.
    W_init = jnp.zeros_like(W_skl)
    tol = 1e-3
    max_iter = 200
    jac_fun = jax.jacrev(fista.fista, argnums=2)
    jac_lam3 = jac_fun(fun_f, W_init, lam, prox_g=None, tol=tol,
                       max_iter=max_iter, acceleration=True, implicit_diff=True)
    self.assertArraysAllClose(jac_lam, jac_lam3, atol=1e-2)

  @parameterized.product(acceleration=[True, False])
  def test_logreg_forward_diff(self, acceleration):
    X, y = datasets.load_digits(return_X_y=True)
    lam = float(X.shape[0])
    tol = 1e-3 if acceleration else 5e-3
    eps = 1e-5
    max_iter = 200
    atol = 1e-3 if acceleration else 1e-1
    fun_f = _make_logreg_objective(X, y)

    jac_lam = (_logreg_skl(X, y, lam + eps) -
               _logreg_skl(X, y, lam - eps)) / (2 * eps)

    # Compute the Jacobian w.r.t. lam via forward differentiation.
    W_init = jnp.zeros((X.shape[1], 10))
    jac_fun = jax.jacfwd(fista.fista, argnums=2)
    jac_lam2 = jac_fun(fun_f, W_init, lam, prox_g=None, tol=tol,
                       max_iter=max_iter, implicit_diff=False,
                       acceleration=acceleration)
    self.assertArraysAllClose(jac_lam, jac_lam2, atol=atol)


if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())